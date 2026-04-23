from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

class StepScreenshots(BaseModel):
    ...


class ValidationIssue(BaseModel):
	"""A single validation issue found during validation"""

	category: Literal['incomplete', 'incorrect', 'loop_detected', 'ambiguous']
	severity: Literal['critical', 'warning', 'info']
	description: str
	affected_steps: list[int] | None = None  # Step numbers where this issue was detected


class ValidationFeedback(BaseModel):
	"""Structured feedback from the validator"""

	is_valid: bool = Field(description='True or False, whether the current state passes validation. ')
	issues: list[ValidationIssue] = Field(default_factory=list, description='List of validation issues found')
	progress_summary: str = Field(description='Summary of what the agent has accomplished so far')
	next_steps_suggestion: str = Field(description='What the agent should do next based on validation')
	recovery_instruction: str | None = Field(
		default=None, description='Specific instruction to recover from issues, if any'
	)
	loop_detected: bool = Field(default=False, description='Whether the agent appears to be looping')
	confidence: float = Field(
		default=1.0, ge=0.0, le=1.0, description='Confidence level of the validation (0-1)'
	)


class ValidationResult(BaseModel):
	"""Complete validation result from the validator agent"""

	step_number: int
	is_periodic: bool = Field(description='True if this is a periodic validation, False if final validation')
	validation_passed: bool = Field(description='Whether validation passed')
	feedback: ValidationFeedback
	should_continue: bool = Field(
		description='Whether the agent should continue (False means task is incomplete/incorrect and needs fixing)'
	)
	reason: str = Field(description='Brief reason for the validation decision')


class ValidatorSettings(BaseModel):
	"""Settings for the validator agent"""

	enabled: bool = Field(default=False, description='Whether to enable the validator hook system')
	validate_every_n_steps: int = Field(
		default=5, ge=1, description='Run periodic validation every N steps'
	)
	validate_on_done: bool = Field(
		default=True, description='Run final validation when agent marks task as done'
	)
	max_validation_retries: int = Field(
		default=2, description='How many times to validate before giving up on specific issue'
	)
	include_screenshots: bool = Field(
		default=True, description='Include screenshots in validation context for vision models'
	)