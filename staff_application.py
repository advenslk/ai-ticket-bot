import json
from datetime import datetime, timezone
from typing import Any

import discord


class StaffApplicationManager:
    """AI-assisted ArveX Hosting staff application workflow.

    The AI collects answers and evaluates them, but never makes the final hiring decision.
    """

    QUESTIONS = [
        ("experience", "Tell me about your previous experience with hosting, VPS, game servers, or server administration."),
        ("linux", "How comfortable are you with Linux/Ubuntu server administration? Mention commands, services, logs, or tasks you can handle."),
        ("docker", "What experience do you have with Docker? If you have used containers in production, explain what you managed."),
        ("pterodactyl", "Do you have experience with Pterodactyl/Wings or similar game-hosting panels? Describe what you have done."),
        ("networking", "How would you troubleshoot a server that is online but a customer's service is unreachable?"),
        ("technical", "What technical skills would you bring to ArveX Hosting? Include programming, automation, networking, security, or infrastructure skills."),
        ("support", "Do you have customer support, Discord moderation, or community management experience? Explain."),
        ("availability", "How many hours can you realistically contribute per week, and what is your usual availability?"),
        ("motivation", "Why do you want to join ArveX Hosting, and what would you improve or contribute to the team?"),
        ("scenario", "A customer reports that their Minecraft server suddenly stopped responding. Give me the troubleshooting steps you would take before escalating it."),
    ]

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.channel_state: dict[int, dict[str, Any]] = {}

    def start(self, channel_id: int, user_id: int) -> dict[str, Any]:
        state = {
            "active": True,
            "user_id": user_id,
            "question_index": 0,
            "answers": {},
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed": False,
            "submitted": False,
        }
        self.channel_state[channel_id] = state
        return state

    def state(self, channel_id: int):
        return self.channel_state.get(channel_id)

    def next_question(self, channel_id: int) -> str | None:
        state = self.state(channel_id)
        if not state:
            return None
        idx = state["question_index"]
        if idx >= len(self.QUESTIONS):
            state["completed"] = True
            return None
        return self.QUESTIONS[idx][1]

    def accept_answer(self, channel_id: int, answer: str) -> tuple[bool, str | None]:
        state = self.state(channel_id)
        if not state or state.get("completed"):
            return False, None
        idx = state["question_index"]
        key, _ = self.QUESTIONS[idx]
        state["answers"][key] = answer.strip()[:5000]
        state["question_index"] += 1
        if state["question_index"] >= len(self.QUESTIONS):
            state["completed"] = True
            return True, None
        return True, self.QUESTIONS[state["question_index"]][1]

    def build_evaluation_prompt(self, state: dict[str, Any]) -> str:
        answers = "\n\n".join(f"{key.upper()}: {value}" for key, value in state.get("answers", {}).items())
        return f"""Evaluate this ArveX Hosting staff application for internal staff review.

IMPORTANT: Do not make the final hiring decision. Give an evidence-based recommendation only.
Evaluate technical ability, practical troubleshooting, communication, reliability signals, hosting experience, and potential risks.
Do not infer protected or sensitive personal attributes. Do not penalize a candidate for age, nationality, religion, race, gender, disability, or other protected characteristics.

Return exactly these sections:
RECOMMENDATION: Strong Candidate / Potential Candidate / Not Recommended
TECHNICAL SCORE: 0-10
SUPPORT SCORE: 0-10
RELIABILITY SCORE: 0-10
STRENGTHS: concise bullet points
CONCERNS: concise bullet points; say None if none are supported by the answers
EXPERIENCE SUMMARY: concise summary
FINAL NOTE: Explain what a human owner/staff member should verify before hiring.

APPLICATION ANSWERS:
{answers}
"""

    def build_review_embed(self, applicant: discord.Member, evaluation: str) -> discord.Embed:
        color = int(self.config.get("embed_color", 10494192))
        emb = discord.Embed(
            title="ArveX Hosting — Staff Application Review",
            description=f"Applicant: {applicant.mention}\n\n{evaluation[:3900]}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        emb.set_footer(text=self.config.get("footer_text", "ArveX Hosting • Staff Recruitment"))
        return emb

    def export_state(self, channel_id: int) -> str:
        return json.dumps(self.state(channel_id) or {}, ensure_ascii=False, indent=2)
