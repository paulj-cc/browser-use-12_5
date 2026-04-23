import json
import logging
import asyncio
import random
from pathlib import Path
# from om2w_judge import encode_image_pil
from PIL import Image
import io, base64, re
from typing import TYPE_CHECKING, List

from .views import (
	ValidationFeedback,
	ValidationIssue,
	ValidationResult,
	ValidatorSettings,
)
from browser_use.llm import ChatOpenAI
from browser_use.llm.messages import SystemMessage, UserMessage

from browser_use.agent.views import AgentHistoryList
from browser_use.browser.views import BrowserStateSummary

logger = logging.getLogger(__name__)

from langfuse import observe

def encode_image_pil(image_path: str) -> str:
    img = Image.open(image_path)
    if img.mode == "RGBA":
        img = img.convert("RGB")
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG")
    return base64.b64encode(buffered.getvalue()).decode("utf-8")


class ValidatorAgent:

	def __init__(
		self,
		task: str,
		validator_llm: ChatOpenAI,
		settings: ValidatorSettings | None = None,
	):
		self.task = task
		self.validator_llm = validator_llm
		self.settings = settings or ValidatorSettings(enabled=True)
		self.logger = logging.getLogger(f'{__name__}.ValidatorAgent')
  
		self._image_cache: dict = {}

	async def validate_periodic(
		self,
		number: int,
		history: AgentHistoryList,
		img_paths: list[str],
		browser_state: BrowserStateSummary | None = None,
	) -> ValidationResult:
		"""
		Periodically validate agent progress during execution.
		
		Args:
			number: Current step number
			history: Agent history up to current step
			browser_state: Current browser state (optional)
		
		Returns:
			ValidationResult with feedback and recommendations
		"""
		self.logger.debug(f'Starting periodic validation at step {number}')

		try:
			feedback = await self._get_validation_feedback(
				number=number,
				history=history,
				browser_state=browser_state,
				validation_type='periodic',
				img_path=img_paths
			)
			print(f'[VALIDATOOOR] valid_periodic 1')
			result = ValidationResult(
				step_number=number,
				is_periodic=True,
				validation_passed=feedback.is_valid,
				feedback=feedback,
				should_continue=True,  # Periodic validation doesn't stop the agent
				reason='Periodic checkpoint validation',
			)

			self._log_validation_result(result)
			return result

		except Exception as e:
			self.logger.error(f'Periodic validation failed: {e}')
			# Return a pass-through result on error to not break the agent
			print(f'[VALIDATOOOR] valid_periodic 2')
			return ValidationResult(
				step_number=number,
				is_periodic=True,
				validation_passed=True,
				feedback=ValidationFeedback(
					is_valid=True,
					progress_summary='Validation skipped due to error',
					next_steps_suggestion='Continue with next step',
					confidence=0.0,
				),
				should_continue=True,
				reason='Validation system error (non-blocking)',
			)

	async def validate_final(
		self,
		number: int,
		history: AgentHistoryList,
		img_paths: list[str],
		browser_state: BrowserStateSummary | None = None,
	) -> ValidationResult:
		"""
		Final validation when agent marks task as done.
		
		Args:
			number: Current step number
			history: Complete agent history
			browser_state: Final browser state (optional)
		
		Returns:
			ValidationResult indicating if task is truly complete
		"""
		self.logger.debug(f'Starting final validation at step {number}')

		try:
			feedback = await self._get_validation_feedback(
				number=number,
				history=history,
				browser_state=browser_state,
				validation_type='final',
				img_path=img_paths,
			)

			print(f'[VALIDATOOOR] valid_fenal 1')
			result = ValidationResult(
				step_number=number,
				is_periodic=False,
				validation_passed=feedback.is_valid,
				feedback=feedback,
				should_continue=not feedback.is_valid,  # Continue if validation failed
				reason='Final task completion validation',
			)

			self._log_validation_result(result)
			return result

		except Exception as e:
			self.logger.error(f'Final validation failed: {e}')
			# On error, assume validation passed to not break completion
			print(f'[VALIDATOOOR] valid_fenal 2')
			return ValidationResult(
				step_number=number,
				is_periodic=False,
				validation_passed=True,
				feedback=ValidationFeedback(
					is_valid=True,
					progress_summary='Unable to perform final validation',
					next_steps_suggestion='Task marked as complete',
					confidence=0.0,
				),
				should_continue=False,
				reason='Validation system error (defaulting to pass)',
			)

	@observe(name='validator._get_validation_feedback')
	async def _get_validation_feedback(
		self,
		number: int,
		history: AgentHistoryList,
		validation_type: str,
		img_path: list[str],
		browser_state: BrowserStateSummary | None = None,
	) -> ValidationFeedback:
		"""Get validation feedback from the LLM"""

		# Build context from history
		history_summary = self._build_history_summary(history)
  
		relevant_images, relevant_thoughts = await self._screen_images(
            subtask=self.task,
            screenshot_paths=img_path,
        )
		# print(f'{"HISTORY SUMMARY":^60}')
		# print(history_summary)
  
		# Build validation prompt
		system_prompt = self._get_system_prompt(validation_type)
		user_prompt = self._get_user_prompt(
			task=self.task,
			history_summary=history_summary,
			browser_state=browser_state,
			validation_type=validation_type,
			relevant_images=relevant_images,
			relevant_thoughts=relevant_thoughts
		)

		messages = [
			SystemMessage(content=system_prompt),
			UserMessage(content=[{"type": "text", "text": user_prompt}] + relevant_images),
		]

		# Call LLM with structured output
		kwargs = {'output_format': ValidationFeedback}
		
		for attempt in range(3):
			try:
				response = await self.validator_llm.ainvoke(messages, **kwargs)
				print(f"response type: {type(response)}")
				break
			except Exception as e:
				delay = (2 ** (attempt + 1)) + random.uniform(0, 1)
				logger.warning("[Validator] Attempt %d failed: %s. Retrying in %.1fs", attempt+1, e, delay)
				await asyncio.sleep(delay)
    
		feedback: ValidationFeedback = response.completion
		print(f'{"="*30}\n{"[ F E E D B A C K ]":^30}\n{feedback}\n{"[ F E E D B A C K ]":^30}\n{"="*30}')
		return feedback

	def _get_system_prompt(self, validation_type: str) -> str:
		"""Get system prompt for the validator"""
		if validation_type == 'final':
			return """You are a task validation expert. Your job is to evaluate whether an AI agent has successfully completed a task.

Review the task requirement and the agent's complete execution history. Determine:
1. Whether the task has been fully completed according to requirements
2. What steps are missing or incomplete
3. Whether the agent made any errors that need correction
4. Your confidence in this assessment

Be strict but fair. The agent must have demonstrably completed all aspects of the task.

**Handling Website Limitations vs. Agent Failures**
Before marking a requirement as unmet, ask: "Was it actually possible to fulfill this on the site the agent was working on?"

A platform limitation is when the website genuinely does not support what the task requires — for example, no price filter exists, or size filters only offer discrete options (55", 65") with no range selector matching the user's request (55"-64"). These are not agent failures.

When a platform limitation is present, check that the agent:
- - Applied the closest available approximation — meaning only the options that fall *within* the user's requested range, never outside it. For example, if the task asks for 55"-64" but the site only offers 55" and 65" as discrete options, the agent should select 55" only, since 65" exceeds the range. The same logic applies to any filter type: prices, ratings, dates, etc. — always stay within bounds, never round outward.
- Acknowledged the limitation explicitly in its reasoning or memory
- Continued to complete all other requirements that were achievable

If the agent did all of the above, mark that requirement as **partially met due to platform limitation** — not as a failure. Only flag it as an agent failure if the agent ignored an available option, misread the UI, or silently skipped a requirement without any acknowledgment."""

		elif validation_type == 'periodic':
			return """You are a task progress validator. Your job is to monitor an AI agent's progress during task execution.

Review the task requirement and the agent's execution history so far. Determine:
1. What the agent has accomplished correctly so far
2. What tasks remain incomplete
3. Whether the agent appears to be looping or stuck
4. What should be done next
5. Your confidence in this assessment

Be helpful and constructive with feedback.

Note that issues should only occur if what the agent has done so far is completely different from the given task. Only create an issue if in the agents run it did something that is not in accordance to the task. If so far it has done things in accordance to the task, even though not all the subtasks are completed, it's likely the agent just hasn't got there yet.

**Handling Website Limitations vs. Agent Failures**
When reviewing progress, distinguish between the agent falling short and the website simply not supporting what the task requires. Signs of a platform limitation include: a filter or option that does not exist in the UI, discrete options that don't cover the user's requested range exactly, or features absent in the local/regional version of the site — all visible in the agent's screenshots. When discrete options are the limitation, the correct behavior is for the agent to select only the options that fall *within* the user's requested range, never outside it (e.g. for a 55"-64" request with only 55" and 65" available, only 55" should be selected). This fallback principle applies to any filter type: the agent should always stay within the user's stated bounds and never overshoot them, even when the available options are coarse.

If you spot a platform limitation, do not flag it as an error. Instead, check whether the agent has acknowledged it and applied the best available workaround. If the agent has not yet encountered or addressed the limitation, note it as something to watch for rather than an active issue."""
		elif validation_type == 'screenshots':
			return """You are an expert evaluator tasked with determining whether an image contains information about the necessary steps to complete a task.

**Objective**: Analyze the provided image and decide if it shows essential steps or evidence required for completing the task. Use your reasoning to explain your decision before assigning a score.

**Instructions**:
1. Provide a detailed description of the image, including its contents, visible elements, text (if any), and any notable features.

2. Carefully examine the image and evaluate whether it contains necessary steps or evidence crucial to task completion:  
- Identify key points that could be relevant to task completion, such as actions, progress indicators, tool usage, applied filters, or step-by-step instructions.  
- Does the image show actions, progress indicators, or critical information directly related to completing the task?  
- Is this information indispensable for understanding or ensuring task success?
- If the image contains partial but relevant information, consider its usefulness rather than dismissing it outright.

3. Provide your response in the following format:  
- **Reasoning**: Explain your thought process and observations. Mention specific elements in the image that indicate necessary steps, evidence, or lack thereof.  
- **Score**: Assign a score based on the reasoning, using the following scale:  
    - **1**: The image does not contain any necessary steps or relevant information.  
    - **2**: The image contains minimal or ambiguous information, unlikely to be essential.  
    - **3**: The image includes some relevant steps or hints but lacks clarity or completeness.  
    - **4**: The image contains important steps or evidence that are highly relevant but not fully comprehensive.  
    - **5**: The image clearly displays necessary steps or evidence crucial for completing the task.

Respond with:  
1. **Reasoning**: [Your explanation]  
2. **Score**: [1-5]"""

	def _get_user_prompt(
		self,
		task: str,
		history_summary: str,
		browser_state: BrowserStateSummary | None,
		validation_type: str,
		relevant_images: list,
		relevant_thoughts: List[str],
	) -> str:
		"""Build the user prompt for validation""" 

		browser_context = ''
		r_thoughts = (
			"\n".join(f"{i+1}. {t}" for i, t in enumerate(relevant_thoughts))
			if relevant_thoughts else ""
		)
		if browser_state:
			browser_context = f"""
Current Browser State:
- URL: {browser_state.url}
- Title: {browser_state.title}
- Number of tabs: {len(browser_state.tabs)}
- Number of interactive elements: {len(browser_state.dom_state.selector_map) if browser_state.dom_state else 0}
"""

		prompt = f"""Task to Complete:
{task}

**Agent Execution History:**
{history_summary}

**Screenshot reasoning (pre-screened, score ≥ 3):**
{r_thoughts}

Based on this information, provide your validation assessment in JSON format with the following structure:
{{
	"is_valid": boolean,
	"issues": [
		{{
			"category": "incomplete|incorrect|loop_detected|ambiguous",
			"severity": "critical|warning|info",
			"description": "description of the issue",
			"affected_steps": [step numbers] or null
		}}
	],
	"progress_summary": "what has been accomplished",
	"next_steps_suggestion": "what should be done next",
	"recovery_instruction": "specific recovery steps if issues found, or null",
	"loop_detected": boolean,
	"confidence": 0.0-1.0
}}

Be concise but thorough. Focus on actual progress toward the task goal."""

		return prompt

	def _build_history_summary(self, history: AgentHistoryList) -> str:
		"""Build a complete per-step summary of agent history for validation."""
		if not history.history:
			return 'No history yet - agent just started'

		lines: list[str] = []

		for step_index, h in enumerate(history.history, start=1):
			lines.append(f'- Step {step_index}')

			# Actions
			if h.model_output and h.model_output.action:
				action_names: list[str] = []
				for action in h.model_output.action:
					action_data = action.model_dump(exclude_unset=True)
					action_name = next(iter(action_data.keys()), 'unknown')
					action_names.append(action_name)
				lines.append(f'  - Actions: {", ".join(action_names)}')
			else:
				lines.append('  - Actions: none')

			# Memory / thought (always include if available)
			if h.model_output and h.model_output.memory:
				lines.append(f'  - Memory: {h.model_output.memory}')
			else:
				lines.append('  - Memory: none')

			# Results / errors
			if h.result:
				for result_index, result in enumerate(h.result, start=1):
					if result.extracted_content is not None:
						lines.append(f'  - Result {result_index}:')
						lines.append(f'{result.extracted_content}')
					if result.error:
						lines.append(f'  - Error {result_index}: {result.error}')
			else:
				lines.append('  - Results: none')

		return '\n'.join(lines)

	async def _screen_images(
		self,
		subtask: str,
		screenshot_paths: list[Path],
	) -> tuple[list, list]:
		"""
		Score each screenshot. Return (image_content_blocks, reasoning_strings)
		for those that pass IMAGE_SCORE_THRESHOLD.
		"""
		
		valid_paths = [Path(p) for p in screenshot_paths if Path(p).exists()]
		new_paths   = [p for p in valid_paths if p not in self._image_cache]
		# Score new screenshots in parallel
		if new_paths:
			b64_list = [encode_image_pil(p) for p in new_paths]
			scores = await asyncio.gather(
				*[self._score_image(subtask, b64, self.validator_llm) for b64 in b64_list],
				return_exceptions=True,
			)
			for path, b64, result in zip(new_paths, b64_list, scores):
				if isinstance(result, Exception):
					logger.warning("[Validator] Image scoring failed for %s: %s", path, result)
					self._image_cache[path] = {"score": 0, "reasoning": "", "b64": b64}
				else:
					score, reasoning = result
					self._image_cache[path] = {"score": score, "reasoning": reasoning, "b64": b64}

		# Collect images that pass the threshold
		relevant_images   = []
		relevant_thoughts = []

		for path in new_paths:
			cached = self._image_cache.get(path, {})
			score  = cached.get("score", 0)
			if score >= 3:
				b64 = cached.get("b64") or encode_image_pil(path)
				relevant_images.append({
					"type": "image_url",
					"image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
				})
				if cached.get("reasoning"):
					relevant_thoughts.append(cached["reasoning"])
				logger.info("[Validator] %s score=%d ✓", path.name, score)
			else:
				logger.info("[Validator] %s score=%d ✗ excluded", path.name, score)

		return relevant_images[:50], relevant_thoughts[:50]

	@observe(name='validator._score_image')
	async def _score_image(
		self, subtask: str, b64: str, llm: ChatOpenAI
	) -> tuple[int, str]:
		"""Score a single screenshot. Returns (score, reasoning)."""
		prompt = (
			f"**task**: {subtask}\n\n"
			"The snapshot of the web page is shown in the image."
		)
		screenshot_llm_prompt = self._get_system_prompt('screenshots')
		messages = [
			SystemMessage(content=screenshot_llm_prompt),
			UserMessage(content=[
				{"type": "text", "text": prompt},
				{"type": "image_url", "image_url": {
					"url": f"data:image/jpeg;base64,{b64}", "detail": "high"
				}},
			]),
		]

		response = await self.validator_llm.ainvoke(messages)

		raw = getattr(response, "completion", "") or getattr(response, "content", "")

		try:
			score_text = raw.split("Score")[1]
			score      = int(re.findall(r"[1-5]", score_text)[0])
			reasoning  = (
				raw.split("**Reasoning**:")[-1].strip()
				.lstrip("\n").split("\n\n")[0].replace("\n", " ")
			)
		except Exception:
			score, reasoning = 0, ""

		return score, reasoning

	def _log_validation_result(self, result: ValidationResult) -> None:
		"""Log validation result"""
		print(f'[log valeed] heheheha')
		status = '✅ PASS' if result.validation_passed else '❌ FAIL'
		self.logger.info(
			f'{status} Validation at step {result.step_number} '
			f'(periodic={result.is_periodic}, confidence={result.feedback.confidence:.1%})'
		)

		if result.feedback.issues:
			self.logger.debug(f'Issues found: {len(result.feedback.issues)}')
			for issue in result.feedback.issues:
				self.logger.debug(f'  - [{issue.severity}] {issue.category}: {issue.description}')