#!/usr/bin/env python3
"""Benchmark monitors-relation-changed downstream write amplification.

This script builds a synthetic monitors relation using the real charm code path,
then measures downstream write amplification for either one steady-state hook or
an incremental setup where units arrive one at a time. It reports the observed
batched relation-set call counts/payload bytes and compares them with the write
counts the previous sequential implementation would have needed.
"""

import argparse
import json
import tempfile
import time
from pathlib import Path
from typing import Dict
from unittest.mock import patch

from ops.model import RelationDataContent
from ops.testing import Harness

from charm import COSProxyCharm

NAGIOS_HOST_CONTEXT = "ubuntu"


def monitor_payload(check_count: int) -> str:
    checks = {f"check_{idx:03d}": f"check_{idx:03d}" for idx in range(check_count)}
    return json.dumps({"monitors": {"remote": {"nrpe": checks}}, "version": "0.3"})


def unit_data(unit_id: int, check_count: int) -> Dict[str, str]:
    addr = f"10.41.{unit_id // 255}.{unit_id % 255}"
    return {
        "egress-subnets": f"{addr}/32",
        "ingress-address": addr,
        "machine_id": str(unit_id),
        "model_id": "fe2c9bbb-58ab-40e4-8f70-f27480093fca",
        "monitors": monitor_payload(check_count),
        "private-address": addr,
        "target-address": addr,
        "target-id": f"{NAGIOS_HOST_CONTEXT}-ubuntu-{unit_id}",
        "nagios_host_context": NAGIOS_HOST_CONTEXT,
    }


def fmt_bytes(value: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value} {unit}"
        value = value / 1024
    raise AssertionError("unreachable")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--units", type=int, default=200)
    parser.add_argument("--checks", type=int, default=3)
    parser.add_argument(
        "--mode",
        choices=("steady-state", "incremental"),
        default="steady-state",
        help=(
            "steady-state measures one changed event after all units are present; "
            "incremental measures adding units one at a time with hooks enabled."
        ),
    )
    parser.add_argument(
        "--commit-ms",
        type=float,
        default=0.0,
        help="Optional modeled fixed latency per relation-set call.",
    )
    parser.add_argument(
        "--kib-ms",
        type=float,
        default=0.0,
        help="Optional modeled latency per KiB sent through relation-set.",
    )
    args = parser.parse_args()

    mock_enrichment_file = Path(tempfile.mkstemp()[1])
    relation_set_calls = 0
    relation_set_key_updates = {"scrape_jobs": 0, "alert_rules": 0}
    relation_set_payload_bytes = {"scrape_jobs": 0, "alert_rules": 0}
    original_commit = RelationDataContent._commit

    def count_commit(databag, data):
        nonlocal relation_set_calls
        if any(key in data for key in relation_set_key_updates):
            relation_set_calls += 1
        for key in relation_set_key_updates:
            if key in data:
                payload_size = len(data[key].encode())
                relation_set_key_updates[key] += 1
                relation_set_payload_bytes[key] += payload_size
        return original_commit(databag, data)

    patches = [
        patch("charm.remove_package"),
        patch.object(COSProxyCharm, "_setup_nrpe_exporter"),
        patch.object(COSProxyCharm, "_start_vector"),
        patch.object(COSProxyCharm, "path", property(lambda *_: mock_enrichment_file)),
        patch("socket.gethostbyaddr", return_value=("localhost", [], ["10.41.0.1"])),
        patch.object(RelationDataContent, "_commit", new=count_commit),
    ]

    for p in patches:
        p.start()

    try:
        harness = Harness(COSProxyCharm)
        harness.add_network("10.41.0.1")
        harness.set_model_info(name="mymodel", uuid="fe2c9bbb-58ab-40e4-8f70-f27480093fca")
        harness.set_leader(True)
        harness.begin_with_initial_hooks()

        rel_id_nrpe = harness.add_relation("monitors", "nrpe")
        rel_id_prom = harness.add_relation("downstream-prometheus-scrape", "prom")
        harness.add_relation_unit(rel_id_prom, "prom/0")

        relation_set_calls = 0
        relation_set_key_updates.update({"scrape_jobs": 0, "alert_rules": 0})
        relation_set_payload_bytes.update({"scrape_jobs": 0, "alert_rules": 0})
        started = time.monotonic()

        if args.mode == "steady-state":
            harness.disable_hooks()
            for unit_id in range(args.units):
                harness.add_relation_unit(rel_id_nrpe, f"nrpe/{unit_id}")
                harness.update_relation_data(
                    rel_id_nrpe, f"nrpe/{unit_id}", unit_data(unit_id, args.checks)
                )
            harness.enable_hooks()

            trigger_data = unit_data(args.units - 1, args.checks)
            trigger_data["benchmark-trigger"] = str(time.monotonic())
            harness.update_relation_data(rel_id_nrpe, f"nrpe/{args.units - 1}", trigger_data)
            sequential_total_calls = args.units * args.checks * 2 + 1
            sequential_key_updates = {
                "scrape_jobs": args.units * args.checks + 1,
                "alert_rules": args.units * args.checks,
            }
        else:
            for unit_id in range(args.units):
                harness.add_relation_unit(rel_id_nrpe, f"nrpe/{unit_id}")
                harness.update_relation_data(
                    rel_id_nrpe, f"nrpe/{unit_id}", unit_data(unit_id, args.checks)
                )
            sequential_key_updates = {
                "scrape_jobs": args.checks * args.units * (args.units + 1) // 2
                + args.units,
                "alert_rules": args.checks * args.units * (args.units + 1) // 2,
            }
            sequential_total_calls = sum(sequential_key_updates.values())

        elapsed = time.monotonic() - started

        final_data = harness.get_relation_data(rel_id_prom, "cos-proxy")
        batched_bytes = {
            "scrape_jobs": len(final_data["scrape_jobs"].encode()),
            "alert_rules": len(final_data["alert_rules"].encode()),
        }
        current_total_key_updates = sum(relation_set_key_updates.values())
        current_total_bytes = sum(relation_set_payload_bytes.values())
        batched_total_bytes = sum(batched_bytes.values())
        if args.mode == "incremental":
            batched_total_bytes *= args.units

        batched_modeled_ms = (
            relation_set_calls * args.commit_ms + current_total_bytes / 1024 * args.kib_ms
        )
        sequential_modeled_ms = (
            sequential_total_calls * args.commit_ms + current_total_bytes / 1024 * args.kib_ms
        )

        print(f"units: {args.units}")
        print(f"checks_per_unit: {args.checks}")
        print(f"mode: {args.mode}")
        print(f"generated_nrpe_targets: {args.units * args.checks}")
        print(f"local_wall_time_seconds: {elapsed:.3f}")
        print()
        print("batched implementation:")
        print(f"  relation_set_calls: {relation_set_calls}")
        print(f"  scrape_jobs_key_updates: {relation_set_key_updates['scrape_jobs']}")
        print(f"  alert_rules_key_updates: {relation_set_key_updates['alert_rules']}")
        print(f"  total_key_updates: {current_total_key_updates}")
        print(f"  relation_set_payload_bytes: {fmt_bytes(current_total_bytes)}")
        print()
        print("sequential estimate:")
        print(f"  relation_set_calls: {sequential_total_calls}")
        print(f"  scrape_jobs_key_updates: {sequential_key_updates['scrape_jobs']}")
        print(f"  alert_rules_key_updates: {sequential_key_updates['alert_rules']}")
        print(f"  total_key_updates: {sum(sequential_key_updates.values())}")
        if args.mode == "incremental":
            print(f"  relation_set_payload_bytes: {fmt_bytes(batched_total_bytes)}")
            print("  byte_estimate: upper bound using the final payload size for every hook")
        else:
            print(f"  relation_set_payload_bytes: {fmt_bytes(batched_total_bytes)}")
        print()
        print("reduction:")
        print(f"  relation_set_calls: {sequential_total_calls / relation_set_calls:.1f}x")
        print(f"  key_updates: {sum(sequential_key_updates.values()) / current_total_key_updates:.1f}x")
        if args.commit_ms or args.kib_ms:
            print()
            print("modeled latency:")
            print(f"  batched_ms: {batched_modeled_ms:.1f}")
            print(f"  sequential_ms: {sequential_modeled_ms:.1f}")
            print(f"  saved_ms: {sequential_modeled_ms - batched_modeled_ms:.1f}")
    finally:
        for p in reversed(patches):
            p.stop()
        mock_enrichment_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
