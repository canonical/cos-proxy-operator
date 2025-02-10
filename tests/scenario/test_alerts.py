from dataclasses import replace
from itertools import chain
from unittest.mock import MagicMock, patch

import pytest
import yaml
from charms.nrpe_exporter.v0.nrpe_exporter import NrpeTargetsChangedEvent
from scenario import Context, Relation, State, StoredState

from charm import COSProxyCharm


@pytest.fixture
def ctx():
    return Context(COSProxyCharm)


def test_base(ctx):
    ctx.run(ctx.on.start(), State())


@pytest.mark.parametrize("n_remote_units", (1, 5, 10))
def test_relation(ctx, n_remote_units):
    monitors_raw = {"nrpe": {"nrpe": "foo"}, "target_id": "bar"}
    monitors = Relation(
        "monitors",
        remote_app_name="remote",
        remote_units_data={
            i: {"monitors": yaml.safe_dump(monitors_raw), "target-id": "hostname-without-unit-id"}
            for i in range(n_remote_units)
        },
    )

    stored_state = StoredState(owner_path="COSProxyCharm", name="_stored", content={})
    state_in = State(leader=True, relations=[monitors], stored_states={stored_state})

    with patch("charm.COSProxyCharm._modify_enrichment_file", new=MagicMock()) as f:
        state_out = ctx.run(ctx.on.relation_changed(relation=monitors), state_in)

    assert f.called
    known_remote_units = set(chain(*(e["target"].keys() for e in f.call_args[1]["endpoints"])))

    assert known_remote_units == {f"remote/{i}" for i in range(n_remote_units)}

    assert isinstance(ctx.emitted_events[1], NrpeTargetsChangedEvent)

    _ = state_out.stored_states

    # simulate pod churn: wipe stored state
    state_after_pod_churn = replace(state_out, stored_states=[])

    with patch("charm.COSProxyCharm._modify_enrichment_file", new=MagicMock()) as f2:
        state_out = ctx.run(ctx.on.relation_changed(monitors), state_after_pod_churn)
    known_remote_units2 = set(chain(*(e["target"].keys() for e in f2.call_args[1]["endpoints"])))

    assert known_remote_units == known_remote_units2

    # simulate filesystem wipe but stored state persists
    state_after_fs_wipe = state_out

    with patch.object(COSProxyCharm, "_modify_enrichment_file", wraps=MagicMock) as f3:
        with ctx(ctx.on.relation_changed(relation=monitors), state_after_fs_wipe) as mgr:
            mgr.run()
            call_args = f3.call_args[1].copy()
            mgr.charm._modify_enrichment_file(call_args)

    known_remote_units3 = set(chain(*(e["target"].keys() for e in call_args["endpoints"])))

    assert known_remote_units == known_remote_units3


@pytest.mark.parametrize("forwarding", (True, False))
def test_forward_alerts(ctx, forwarding):
    prometheus_relation = Relation(
        "downstream-prometheus-scrape",
        remote_app_name="prometheus",
    )

    stored_state = StoredState(
        owner_path="COSProxyCharm/MetricsEndpointAggregator[downstream-prometheus-scrape]",
        name="_stored",
        content={"alert_rules": [{"name": "foo", "rules": []}]},
    )
    state_in = State(
        leader=True,
        relations=[prometheus_relation],
        stored_states={stored_state},
        config={"forward_alert_rules": forwarding},
    )

    with patch("charm.COSProxyCharm._modify_enrichment_file", new=MagicMock()):
        state_out = ctx.run(ctx.on.config_changed(), state_in)
        prometheus_relation_out = state_out.get_relation(prometheus_relation.id)
        if forwarding:
            assert prometheus_relation_out.local_app_data["alert_rules"] != '{"groups": []}'
        else:
            assert prometheus_relation_out.local_app_data["alert_rules"] == '{"groups": []}'
