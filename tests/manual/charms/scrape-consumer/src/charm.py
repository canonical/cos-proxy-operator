#!/usr/bin/env python3

import json

from ops import ActiveStatus, CharmBase, WaitingStatus, main


class ScrapeConsumerCharm(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.downstream_prometheus_scrape_relation_joined, self._update)
        self.framework.observe(self.on.downstream_prometheus_scrape_relation_changed, self._update)
        self.unit.status = WaitingStatus("waiting for scrape data")

    def _update(self, _):
        for relation in self.model.relations["downstream-prometheus-scrape"]:
            if not relation.app:
                continue
            data = relation.data[relation.app]
            scrape_jobs = json.loads(data.get("scrape_jobs", "[]"))
            alert_rules = json.loads(data.get("alert_rules", "{}"))
            groups = alert_rules.get("groups", [])
            rule_count = sum(len(group.get("rules", [])) for group in groups)
            self.unit.status = ActiveStatus(
                f"scrape_jobs={len(scrape_jobs)} alert_rules={rule_count}"
            )
            return

        self.unit.status = WaitingStatus("waiting for scrape data")


if __name__ == "__main__":
    main(ScrapeConsumerCharm)
