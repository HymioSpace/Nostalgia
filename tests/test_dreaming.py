from dataclasses import replace

from nostalgia.dreaming import DreamScheduler


class ProposalDreamer:
    def __init__(self):
        self.tasks = []

    def propose(self, task):
        self.tasks.append(task)
        record = task["records"][0]
        return {
            "artifacts": [
                {
                    "id": "food-preference",
                    "content": record["context"]["text"],
                    "topics": ["food"],
                    "references": [
                        {"root_id": record["root_id"], "record_id": record["id"]}
                    ],
                },
                {
                    "id": "_index",
                    "content": "Food preference is available in food-preference.",
                    "links": ["food-preference"],
                },
            ],
            "record_states": [
                {
                    "record_id": row["id"],
                    "processing_state": "applied",
                    "status": "active",
                }
                for row in task["records"]
            ],
        }


def test_periodic_dream_cycle_commits_inside_nostalgia(api, app, settings):
    record = api.context("我喜欢吃辣。")
    dreamer = ProposalDreamer()
    scheduler = DreamScheduler(
        replace(
            settings,
            dreaming_enabled=True,
            dreaming_interval_seconds=600,
            dreaming_provider_url="http://provider.invalid/dream",
        ),
        app.state.store,
        app.state.core,
        dreamer=dreamer,
    )
    result = scheduler.run_once()
    assert result[0]["state"] == "committed"
    assert [item["id"] for item in dreamer.tasks[0]["records"]] == [record["id"]]
    card = api.get("/v1/artifacts/food-preference").json()
    assert card["content"] == "我喜欢吃辣。"
    assert card["references"][0]["record_id"] == record["id"]
    assert api.get("/v1/injection").json()["summary"]["content"].startswith(
        "Food preference"
    )
    assert scheduler.run_once() == []
