"""NeMo component entrypoint; set NEMO_GYM_EXTRA_ROOTS to the project root."""
from integrations.nemo_skill_agent import SkillGeneratorAgent

if __name__ == "__main__":
    SkillGeneratorAgent.run_webserver()
