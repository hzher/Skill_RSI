"""PAST-Bench tasks through its original orchestration, services and grader."""
from .clbench import CLBench
from .files import contained


class PASTBench(CLBench):
    worker_file = "pastbench_worker.py"
    scorer_name = "pastbench"

    def __init__(self, config, base):
        tasks = []
        for row in config["tasks"]:
            parameters = {"task": row["task"], "sandbox": row.get("sandbox", False),
                          "port_offset": row.get("port_offset", 0), "judge": config.get("judge", True),
                          "config": config.get("config")}
            tasks.append({"id": row["id"], "family": "pastbench", "parameters": parameters})
        super().__init__({**config, "tasks": tasks}, base)
        for spec in self.specs.values():
            parameters = spec["parameters"]
            path = contained(self.repository, parameters["task"])
            if not path.is_dir() or not (path / "task.yaml").is_file():
                raise ValueError("PAST-Bench selection must name an original task directory")
            if parameters["config"]:
                parameters["config"] = str(contained(self.repository, parameters["config"]))

    def configuration(self):
        return {**super().configuration(), "kind": "pastbench", "protocol": "official-task-runtime-v1",
                "sequence_persistence": "not_supported"}
