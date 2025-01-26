from typing import Any, Dict, List, Optional, Union, TypeVar, Generic, Callable, Protocol, cast
from dataclasses import dataclass
from uuid import uuid4
import asyncio
from abc import ABC, abstractmethod
import json

from .base import BaseLLMInterface, LLMResponse
from ..prompts.loader import PromptLoader
from ..utils.storage import StorageManager

T = TypeVar('T')
ChainResult = TypeVar('ChainResult')

class InputTransform(Protocol):
    def __call__(self, context: 'ChainContext', **kwargs: Any) -> Dict[str, Any]: ...

class OutputTransform(Protocol):
    def __call__(self, response: LLMResponse) -> T: ...

@dataclass
class ChainContext:
    """Context object passed between chain steps."""
    artifacts: Dict[str, Any]  # Intermediate results
    metadata: Dict[str, Any]   # Chain execution metadata
    state: Dict[str, Any]      # Current chain state

class ChainStep(Generic[T]):
    """Represents a single step in a prompt chain."""
    def __init__(
        self, 
        task_type: str,
        input_transform: Optional[InputTransform] = None,
        output_transform: Optional[OutputTransform] = None,
        retry_strategy: Optional[Dict[str, Any]] = None
    ):
        self.id = str(uuid4())
        self.task_type = task_type
        self.input_transform = input_transform
        self.output_transform = output_transform
        self.retry_strategy = retry_strategy or {"max_retries": 3}
        
    async def execute(
        self, 
        llm: BaseLLMInterface,
        prompt_loader: PromptLoader,
        context: ChainContext,
        **kwargs: Any
    ) -> T:
        """Execute this chain step."""
        # Transform input using context if needed
        input_data = kwargs
        if self.input_transform:
            input_data = self.input_transform(context, **kwargs)
            
        # Get and format prompt
        system_prompt, user_prompt = prompt_loader.format_prompt(
            self.task_type,
            **input_data
        )
        
        # Execute LLM call with retry
        retries = 0
        while True:
            try:
                response = await llm.process(user_prompt, system_prompt=system_prompt)
                break
            except Exception as e:
                retries += 1
                if retries >= self.retry_strategy["max_retries"]:
                    raise
                await asyncio.sleep(self.retry_strategy.get("delay", 1))
        
        # Transform output if needed
        if self.output_transform:
            return self.output_transform(response)
        return cast(T, response)

class AbstractChain(ABC, Generic[ChainResult]):
    """Abstract base class for prompt chains."""
    
    def __init__(
        self,
        llm: BaseLLMInterface,
        storage: Optional[StorageManager] = None,
        prompt_loader: Optional[PromptLoader] = None,
        base_path: str = "./chain_storage"
    ):
        self.llm = llm
        self.storage = storage or StorageManager(base_path)
        self.prompt_loader = prompt_loader or PromptLoader()
        self.context = ChainContext(
            artifacts={},
            metadata={"chain_id": str(uuid4())},
            state={}
        )
        
    @abstractmethod
    async def execute(self, **kwargs: Any) -> ChainResult:
        """Execute the chain."""
        pass
    
    def store_artifact(self, name: str, data: Any) -> None:
        """Store an intermediate artifact."""
        self.context.artifacts[name] = data
        if self.storage:
            # Serialize data to JSON for storage
            serialized = json.dumps(data)
            with open(f"{self.storage.base_path}/{self.context.metadata['chain_id']}/{name}.json", "w") as f:
                f.write(serialized)
            
    def get_artifact(self, name: str) -> Optional[Any]:
        """Retrieve a stored artifact."""
        if name in self.context.artifacts:
            return self.context.artifacts[name]
        if self.storage:
            try:
                with open(f"{self.storage.base_path}/{self.context.metadata['chain_id']}/{name}.json", "r") as f:
                    return json.loads(f.read())
            except (FileNotFoundError, json.JSONDecodeError):
                return None
        return None

class SequentialChain(AbstractChain[ChainResult]):
    """A chain that executes steps sequentially."""
    
    def __init__(
        self,
        steps: List[ChainStep[ChainResult]],
        *args,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.steps = steps
        
    async def execute(self, **kwargs: Any) -> ChainResult:
        """Execute steps sequentially."""
        result: Optional[ChainResult] = None
        for step in self.steps:
            result = await step.execute(
                self.llm,
                self.prompt_loader,
                self.context,
                **kwargs
            )
            self.store_artifact(f"step_{step.id}", result)
            kwargs = {"previous_result": result}  # Pass result to next step
        if result is None:
            raise ValueError("Chain execution produced no result")
        return result

class ParallelChain(AbstractChain[List[ChainResult]]):
    """A chain that executes steps in parallel."""
    
    def __init__(
        self,
        steps: List[ChainStep],
        max_concurrent: Optional[int] = None,
        *args,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.steps = steps
        self.max_concurrent = max_concurrent
        
    async def execute(self, **kwargs) -> List[ChainResult]:
        """Execute steps in parallel."""
        if self.max_concurrent:
            # Use semaphore to limit concurrency
            sem = asyncio.Semaphore(self.max_concurrent)
            async def bounded_execute(step: ChainStep):
                async with sem:
                    return await step.execute(
                        self.llm,
                        self.prompt_loader,
                        self.context,
                        **kwargs
                    )
            tasks = [bounded_execute(step) for step in self.steps]
        else:
            tasks = [
                step.execute(
                    self.llm,
                    self.prompt_loader,
                    self.context,
                    **kwargs
                )
                for step in self.steps
            ]
            
        results = await asyncio.gather(*tasks)
        for step, result in zip(self.steps, results):
            self.store_artifact(f"step_{step.id}", result)
        return results

class ChordChain(AbstractChain[ChainResult]):
    """A chain that executes parallel steps followed by a callback."""
    
    def __init__(
        self,
        parallel_steps: List[ChainStep],
        callback_step: ChainStep,
        max_concurrent: Optional[int] = None,
        *args,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.parallel_chain = ParallelChain(
            parallel_steps,
            max_concurrent,
            llm=self.llm,
            storage=self.storage,
            prompt_loader=self.prompt_loader
        )
        self.callback_step = callback_step
        
    async def execute(self, **kwargs) -> ChainResult:
        """Execute parallel steps then callback."""
        # Execute parallel steps
        parallel_results = await self.parallel_chain.execute(**kwargs)
        self.store_artifact("parallel_results", parallel_results)
        
        # Execute callback with parallel results
        callback_result = await self.callback_step.execute(
            self.llm,
            self.prompt_loader,
            self.context,
            parallel_results=parallel_results,
            **kwargs
        )
        self.store_artifact(f"step_{self.callback_step.id}", callback_result)
        
        return callback_result

class GroupChain(AbstractChain[List[ChainResult]]):
    """A chain that groups multiple chains to be executed together."""
    
    def __init__(
        self,
        chains: List[AbstractChain],
        *args,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.chains = chains
        
    async def execute(self, **kwargs) -> List[ChainResult]:
        """Execute all chains in the group."""
        tasks = [chain.execute(**kwargs) for chain in self.chains]
        results = await asyncio.gather(*tasks)
        self.store_artifact("group_results", results)
        return results

class MapChain(AbstractChain[List[ChainResult]]):
    """A chain that maps a single chain across multiple inputs."""
    
    def __init__(
        self,
        chain_template: AbstractChain,
        *args,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.chain_template = chain_template
        
    async def execute(self, inputs: List[Dict[str, Any]], **kwargs) -> List[ChainResult]:
        """Execute the template chain for each input."""
        tasks = []
        for input_data in inputs:
            # Create new chain instance for each input
            chain = self.chain_template.__class__(
                llm=self.llm,
                storage=self.storage,
                prompt_loader=self.prompt_loader
            )
            # Merge input specific data with common kwargs
            chain_kwargs = {**kwargs, **input_data}
            tasks.append(chain.execute(**chain_kwargs))
            
        results = await asyncio.gather(*tasks)
        self.store_artifact("map_results", results)
        return results

class ReminderChain(AbstractChain[ChainResult]):
    """A chain that maintains context by injecting initial prompt into subsequent steps."""
    
    def __init__(
        self,
        steps: List[ChainStep[ChainResult]],
        reminder_template: str = "Remember the original task: {initial_prompt}\nPrevious context: {context}\n\nCurrent task: {current_prompt}",
        prune_completed: bool = True,
        completion_markers: Optional[List[str]] = None,
        max_context_items: Optional[int] = None,
        *args,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.steps = steps
        self.reminder_template = reminder_template
        self.initial_prompt = None
        self.context_history = []
        self.prune_completed = prune_completed
        self.completion_markers = completion_markers or [
            "completed", "finished", "done",
            "conclusion", "final", "summary"
        ]
        self.max_context_items = max_context_items
        
    def _is_step_completed(self, result: LLMResponse) -> bool:
        """Check if a step's result indicates completion."""
        if not self.prune_completed:
            return False
            
        content = result.content.lower()
        return any(marker in content for marker in self.completion_markers)
        
    def _prune_context(self) -> List[LLMResponse]:
        """Remove completed steps from context history."""
        if not self.prune_completed:
            return self.context_history
            
        # Keep steps that aren't marked as completed
        pruned = [
            result for result in self.context_history 
            if not self._is_step_completed(result)
        ]
        
        # If max_context_items is set, keep only the most recent items
        if self.max_context_items and len(pruned) > self.max_context_items:
            pruned = pruned[-self.max_context_items:]
            
        return pruned
        
    def _create_reminder_transform(self, original_transform: Optional[InputTransform] = None) -> InputTransform:
        """Create an input transform that injects the reminder."""
        def reminder_transform(context: ChainContext, **kwargs: Any) -> Dict[str, Any]:
            # Get transformed input if original transform exists
            input_data = original_transform(context, **kwargs) if original_transform else kwargs
            
            # Get the current prompt
            current_prompt = input_data.get("prompt", "")
            
            # Store initial prompt if this is the first step
            if self.initial_prompt is None:
                self.initial_prompt = current_prompt
            
            # Get pruned context history
            pruned_history = self._prune_context()
            
            # Build context from pruned history
            context_str = "\n".join(
                f"Step {i+1}: {result.content}"
                for i, result in enumerate(pruned_history)
            )
            
            # Create reminder prompt
            reminder_prompt = self.reminder_template.format(
                initial_prompt=self.initial_prompt,
                context=context_str,
                current_prompt=current_prompt
            )
            
            # Update input data with reminder
            input_data["prompt"] = reminder_prompt
            return input_data
            
        return reminder_transform
    
    async def execute(self, **kwargs: Any) -> ChainResult:
        """Execute steps with context reminders."""
        result: Optional[ChainResult] = None
        
        for step in self.steps:
            # Create a new step with reminder transform
            reminded_step = ChainStep[ChainResult](
                task_type=step.task_type,
                input_transform=self._create_reminder_transform(step.input_transform),
                output_transform=step.output_transform,
                retry_strategy=step.retry_strategy
            )
            
            # Execute step
            result = await reminded_step.execute(
                self.llm,
                self.prompt_loader,
                self.context,
                **kwargs
            )
            
            # Store result in history and artifacts
            self.context_history.append(result)
            self.store_artifact(f"step_{step.id}", result)
            
            # Update kwargs for next step
            kwargs = {
                "previous_result": result,
                "prompt": kwargs.get("prompt", "")  # Preserve original prompt
            }
        
        if result is None:
            raise ValueError("Chain execution produced no result")
        return result

