#!/usr/bin/env python3

import json

from ops import ActiveStatus, CharmBase, main


class MonitorsProviderCharm(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.config_changed, self._publish)
        self.framework.observe(self.on.monitors_relation_joined, self._publish)

    def _publish(self, _):
        checks = int(self.config["checks"])
        unit_id = int(self.unit.name.split("/")[1])
        address = self.model.get_binding("monitors").network.bind_address
        monitors = {
            "version": "0.3",
            "monitors": {
                "remote": {
                    "nrpe": {
                        f"check_{idx:03d}": f"check_{idx:03d}" for idx in range(checks)
                    }
                }
            },
        }

        for relation in self.model.relations["monitors"]:
            relation.data[self.unit].update(
                {
                    "egress-subnets": f"{address}/32",
                    "ingress-address": str(address),
                    "machine_id": str(unit_id),
                    "model_id": self.model.uuid,
                    "monitors": json.dumps(monitors),
                    "private-address": str(address),
                    "target-address": str(address),
                    "target-id": f"benchmark-monitors-provider-{unit_id}",
                    "nagios_host_context": "benchmark",
                }
            )

        self.unit.status = ActiveStatus(f"checks={checks}")


if __name__ == "__main__":
    main(MonitorsProviderCharm)
