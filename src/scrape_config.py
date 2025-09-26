"""Prometheus scrape config definition."""
import logging
import socket
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class ScrapeConfig:
    """A Prometheus scrape config instance.

    The config this class provides is a subset of the upstream format:
    https://prometheus.io/docs/prometheus/latest/configuration/configuration/#scrape_config
    """

    def __init__(
        self,
        model_name: str,
        model_uuid: str,
        relabel_instance=True,
        resolve_addresses=False,
    ):
        self._model_name = model_name
        self._model_uuid = model_uuid
        self._relabel_instance = relabel_instance
        self._resolve_addresses = resolve_addresses

    def job_name(self, appname: str) -> str:
        """Construct a scrape job name.

        Each relation has its own unique scrape job name. All units in
        the relation are scraped as part of the same scrape job.

        Args:
            appname: string name of a related application.

        Returns:
            a string Prometheus scrape job name for the application.
        """
        return f"juju_{self._model_name}_{self._model_uuid[:7]}_{appname}_prometheus_scrape"

    def from_targets(self, targets, application_name: str, **kwargs) -> dict:
        """Construct a static scrape job for an application from targets.

        Args:
            targets: a dictionary providing hostname and port for all scrape target. The keys of
                this dictionary are unit names. Values corresponding to these keys are themselves
                a dictionary with keys "hostname" and "port".
            application_name: a string name of the application for which this static scrape job is
                being constructed.
            kwargs: a `dict` of the extra arguments passed to the function

        Returns:
            A dictionary corresponding to a Prometheus static scrape job configuration for one
            application. The returned dictionary may be transformed into YAML and appended to the
            list of any existing list of Prometheus static configs.
        """
        job = {
            "job_name": self.job_name(application_name),
            "static_configs": [
                {
                    "targets": ["{}:{}".format(target["hostname"], target["port"])],
                    "labels": {
                        "juju_model": self._model_name,
                        "juju_model_uuid": self._model_uuid,
                        "juju_application": application_name,
                        "juju_unit": unit_name,
                        "host": target["hostname"],
                        # Expanding this will merge the dicts and replace the
                        # topology labels if any were present/found
                        **self._static_config_extra_labels(target),
                    },
                }
                for unit_name, target in targets.items()
            ],
            "relabel_configs": self.relabel_configs() + kwargs.get("relabel_configs", []),
        }
        job.update(kwargs.get("updates", {}))

        return job

    def _static_config_extra_labels(self, target: Dict[str, str]) -> Dict[str, str]:
        """Build a list of extra static config parameters, if specified."""
        extra_info = {}

        if self._resolve_addresses:
            try:
                dns_name = socket.gethostbyaddr(target["hostname"])[0]
            except OSError:
                logger.debug("Could not perform DNS lookup for %s", target["hostname"])
                dns_name = target["hostname"]
            extra_info["dns_name"] = dns_name

        return extra_info

    def relabel_configs(self) -> List[Dict[str, Any]]:
        """Create Juju topology relabeling configuration.

        Using Juju topology for instance labels ensures that these
        labels are stable across unit recreation.

        Returns:
            a list of Prometheus relabeling configurations. Each item in
            this list is one relabel configuration.
        """
        return (
            [
                {
                    "source_labels": [
                        "juju_model",
                        "juju_model_uuid",
                        "juju_application",
                        "juju_unit",
                    ],
                    "separator": "_",
                    "target_label": "instance",
                    "regex": "(.*)",
                }
            ]
            if self._relabel_instance
            else []
        )
