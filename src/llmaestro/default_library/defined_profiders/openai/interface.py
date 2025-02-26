"""OpenAI interface implementation."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Union, AsyncIterator, TYPE_CHECKING, cast, overload, Tuple

from litellm import acompletion, completion

from llmaestro.core.models import LLMResponse, TokenUsage
from llmaestro.llm.interfaces.base import BaseLLMInterface, ImageInput
from llmaestro.prompts.base import BasePrompt
from llmaestro.prompts.types import PromptMetadata, ResponseFormat, ResponseFormatType, VersionInfo

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class OpenAIInterface(BaseLLMInterface):
    """OpenAI-specific implementation of the LLM interface."""

    @property
    def model_family(self) -> str:
        """Get the model family."""
        return "openai"

    async def initialize(self) -> None:
        """Initialize async components of the interface."""
        await super().initialize()
        # Additional OpenAI-specific initialization can go here

    @overload
    async def process(self, prompt: str, variables: Optional[Dict[str, Any]] = None) -> LLMResponse:
        ...

    @overload
    async def process(self, prompt: BasePrompt, variables: Optional[Dict[str, Any]] = None) -> LLMResponse:
        ...

    async def process(
        self,
        prompt: Union[BasePrompt, str],
        variables: Optional[Dict[str, Any]] = None,
    ) -> LLMResponse:
        """Process input using OpenAI's API via LiteLLM."""
        try:
            # Convert string prompt to BasePrompt if needed
            if isinstance(prompt, str):
                prompt = BasePrompt(
                    name="direct_prompt",
                    description="Direct string prompt",
                    system_prompt="",
                    user_prompt=prompt,
                    metadata=PromptMetadata(
                        type="direct_input",
                        expected_response=ResponseFormat(format=ResponseFormatType.TEXT, schema=None),
                        tags=[],
                    ),
                    current_version=VersionInfo(
                        number="1.0.0",
                        author="system",
                        description="Direct string prompt",
                        timestamp=datetime.now(),
                        change_type="initial",
                    ),
                )

            # Render the prompt with optional variables
            system_prompt, user_prompt, attachments = prompt.render(**(variables or {}))

            # Convert attachments to ImageInput objects if any
            images = (
                [
                    ImageInput(content=att["content"], media_type=att["mime_type"], file_name=att["file_name"])
                    for att in attachments
                ]
                if attachments
                else None
            )

            # Format messages
            messages = self._format_messages(input_data=user_prompt, system_prompt=system_prompt, images=images)

            if self.state:
                model_name = self.state.profile.name
                max_tokens = self.state.runtime_config.max_tokens
                temperature = self.state.runtime_config.temperature
            else:
                raise ValueError("No state provided, a LLMState must be provided to the LLMInterface")

            # Make API call
            response = await acompletion(
                messages=messages,
                model=model_name,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=False,
            )

            return await self._handle_response(response)

        except Exception as e:
            return self._handle_error(e)

    async def batch_process(
        self,
        prompts: List[Union[BasePrompt, str]],
        variables: Optional[List[Optional[Dict[str, Any]]]] = None,
        batch_size: Optional[int] = None,
    ) -> List[LLMResponse]:
        """Batch process prompts using OpenAI's API."""
        # Ensure variables list matches prompts length if provided
        if variables is not None and len(variables) != len(prompts):
            raise ValueError("Number of variable sets must match number of prompts")

        # Process each prompt
        results = []
        for i, prompt in enumerate(prompts):
            # Use corresponding variables if provided, otherwise None
            prompt_vars = variables[i] if variables is not None else None
            result = await self.process(prompt, prompt_vars)
            results.append(result)

        return results

    async def stream(
        self,
        prompt: Union[BasePrompt, str],
        variables: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[LLMResponse]:
        """Stream responses from OpenAI's API one token at a time.

        Args:
            prompt: The prompt to process
            variables: Optional variables for prompt templating

        Returns:
            An async iterator yielding partial LLMResponses as they are generated
        """
        try:
            # Convert string prompt to BasePrompt if needed
            if isinstance(prompt, str):
                prompt = BasePrompt(
                    name="direct_prompt",
                    description="Direct string prompt",
                    system_prompt="",
                    user_prompt=prompt,
                    metadata=PromptMetadata(
                        type="direct_input",
                        expected_response=ResponseFormat(format=ResponseFormatType.TEXT, schema=None),
                        tags=[],
                    ),
                    current_version=VersionInfo(
                        number="1.0.0",
                        author="system",
                        description="Direct string prompt",
                        timestamp=datetime.now(),
                        change_type="initial",
                    ),
                )

            # Render the prompt with optional variables
            system_prompt, user_prompt, attachments = prompt.render(**(variables or {}))

            # Convert attachments to ImageInput objects if any
            images = (
                [
                    ImageInput(content=att["content"], media_type=att["mime_type"], file_name=att["file_name"])
                    for att in attachments
                ]
                if attachments
                else None
            )

            # Format messages
            messages = self._format_messages(input_data=user_prompt, system_prompt=system_prompt, images=images)

            if not self.state:
                raise ValueError("No state provided, a LLMState must be provided to the LLMInterface")

            # Get model configuration
            model_name = self.state.profile.name
            max_tokens = self.state.runtime_config.max_tokens
            temperature = self.state.runtime_config.temperature

            # Make streaming API call
            response = completion(
                messages=messages,
                model=model_name,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=True,
            )

            # Process the streaming response
            for chunk in response:
                # litellm returns a tuple of (content, metadata) for streaming
                content, _ = cast(Tuple[str, Any], chunk)
                if content:
                    yield LLMResponse(
                        content=content,
                        success=True,
                        token_usage=TokenUsage(
                            completion_tokens=1,  # Each chunk is roughly one token
                            prompt_tokens=0,  # We don't know prompt tokens in streaming
                            total_tokens=1,
                        ),
                        context_metrics=self._calculate_context_metrics(),
                        metadata={
                            "model": self.state.profile.name,
                            "is_streaming": True,
                            "is_partial": True,
                        },
                    )

        except Exception as e:
            yield self._handle_error(e)

    def _handle_error(self, e: Exception) -> LLMResponse:
        """Handle errors in LLM processing."""
        error_message = f"Error processing LLM request: {str(e)}"
        logger.error(error_message, exc_info=True)
        return LLMResponse(
            content="",
            success=False,
            error=str(e),
            metadata={"error_type": type(e).__name__},
            token_usage=TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        )
