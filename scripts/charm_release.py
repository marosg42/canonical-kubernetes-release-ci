"""
Script to automate the k8s-operator charms release process.

The implementation works as a state machine that queries the current state of each track
and then decides what to do next.The script is designed to be idempotent, meaning that it
can be run multiple times without causing any harm.

For each track to be published on stable from candidate, there are more than one revisions
that need to be tested, each corresponding to a unique (arch, base) of each charm for which
a revision has been published. We call the set of all such revisions a revision matrix of a
track. For example track 1.32 of k8s can have the following matrix of revisions on candidate
risk level:

        20.04   22.04   24.04
amd64   741     742     743
arm64   736     748     750

And the following matrix of revisions published on stable:

        20.04   22.04   24.04
amd64   456     457     458
arm64   459     460     461

The goal is to promote all 6 revisions of the 1.32/candidate to 1.32/stable. The same goes for
k8s-worker charm and any other charm that might be needed to have a complete test. We call the
revision matrices of all the necessary charms a bundle, which should not be confused with charm
bundles.

for each track:
    for each charm:
        get data for current track state:
        - extract all the revisions corresponding to each (arch, base) published on channel=<track>/candidate
        - extract all the revisions corresponding to each (arch, base) published on stable_channel=<track>/stable

    skip if all the charms have their revisions on <track>/candidate already published in <track>/stable

    for each (arch, base):
        extract the corresponding revision of each charm on channel=<track>/candidate
        try the following reconciliation pattern:

        revision is in one of the following states:
            - no TPIs yet -> NO_TEST
            - at least one TPI succeeded -> TEST_SUCCESS
            - at least one TPI in progress -> TEST_IN_PROGRESS
            - there are only failed/(in-)error TPIs -> TEST_FAILED

        Actions:
            - NO_TEST: start a new TPI
            - TEST_IN_PROGRESS: just print that as a log message
            - TEST_SUCCESS: promote the charm revisions to the next channel
            - TEST_FAILED: manual intervention with SQA required

    aggregate the results for all the revisions checked for each track:
        - If all revisions have TEST_SUCCESS then report the track state as succeeded
        - If any of the revisions have TEST_FAILED then report the track state as failed
        - If some of the tests are still in TEST_IN_PROGRESS report the track as in progress

TODOs:
* Support testing different architectures once SQA provides the feature
* Cleaning up outdated and aborted TPIs in a separate cronjob
"""

import argparse
import logging
from enum import StrEnum, auto
from typing import Dict

from requests.exceptions import HTTPError
from util import charmhub, k8s, sqa

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class TrackState:
    def __init__(self):
        self._state_map: Dict[str, sqa.TestPlanInstanceStatus] = {}

    def set_state(self, version, state: sqa.TestPlanInstanceStatus):
        self._state_map[version] = state

    def __str__(self):
        return str([(key, str(value)) for key, value in self._state_map.items()])

    @property
    def empty(self) -> bool:
        return not self._state_map

    @property
    def failed(self) -> bool:
        return any(s.failed for s in self._state_map.values())

    @property
    def succeeded(self) -> bool:
        if self.empty:
            return False
        return all(s.succeeded for s in self._state_map.values())

    @property
    def in_progress(self) -> bool:
        if self.failed:
            return False
        return any(s.in_progress for s in self._state_map.values())


class ProcessState(StrEnum):
    PROCESS_SUCCESS = auto()
    PROCESS_IN_PROGRESS = auto()
    PROCESS_FAILED = auto()
    PROCESS_CI_FAILED = auto()
    PROCESS_UNCHANGED = auto()

def ensure_track_state(
    channel, bundle: charmhub.Bundle, dry_run: bool, priority_generator: sqa.PriorityGenerator
) -> TrackState:
    track_state = TrackState()
    for arch in bundle.get_archs():
        # Note(Reza): Currently SQA only supports the test for the amd64 architecture
        # we should differentiate the TPIs for different architectures once arm64 is
        # also supported. I have not put that in a file to avoid creating a perception
        # that more than one architecture could be tested. Having more than one arch
        # would break the pipeline by creating duplicates as there are no ways to
        # distinguish test environments for architectures on SQA side.
        if arch != "amd64":
            continue

        for base in bundle.get_bases():
            version = bundle.get_version(arch, base)
            if not version:
                continue
            priority = priority_generator.next_priority
            log.info(f"Checking if there is any TPIs for ({channel}, {arch}, {base}, {priority})")
            current_test_plan_instance_status = sqa.current_test_plan_instance_status(
                channel, base, version
            )
            if not current_test_plan_instance_status:
                revisions = bundle.get_revisions(arch, base)
                # We are creating TPIs with different priorities to avoid overloading the
                # SQA platform

                log.info(f"No TPI found. Creating a new TPI for {revisions} with priority {priority}")

                if not dry_run:
                    sqa.start_release_test(channel, base, arch, revisions, version, priority)

                track_state.set_state(version, sqa.TestPlanInstanceStatus.IN_PROGRESS)
                continue

            track_state.set_state(version, current_test_plan_instance_status)

    return track_state


def process_track(track: str, priority_generator: sqa.PriorityGenerator, args) -> ProcessState:
    """Process the given track based on its current state."""

    dry_run: bool = args.dry_run
    bundle_charms: list[str] = args.charms
    from_channel = f"{track}/{args.from_risk}"
    to_channel = f"{track}/{args.to_risk}"
    k8s_operator_bundle = charmhub.Bundle("k8s-operator")
    at_least_one_charm = False
    for charm in bundle_charms:
        log.info(f"Getting revisions for {charm} charm on track {track}")
        try:
            from_revision_matrix = charmhub.get_revision_matrix(charm, from_channel)
        except HTTPError:
            log.exception(f"failed to get revision matrix for charm {charm} channel={from_channel}")
            return ProcessState.PROCESS_CI_FAILED
        log.info("Channel %s revisions:\n %s", from_channel, from_revision_matrix)

        try:
            stable_revision_matrix = charmhub.get_revision_matrix(charm, to_channel)
        except HTTPError:
            log.exception(f"failed to get revision matrix for charm {charm} channel={to_channel}")
            return ProcessState.PROCESS_CI_FAILED
        log.info("Channel %s revisions:\n %s", to_channel, stable_revision_matrix)

        if not from_revision_matrix:
            log.info(f"The channel {from_channel} of {charm} has no revisions.")
            k8s_operator_bundle.set(charm, stable_revision_matrix)
            continue

        if from_revision_matrix == stable_revision_matrix:
            log.info(
                f"The channel {from_channel} of {charm} is already published in {to_channel}."
            )
            k8s_operator_bundle.set(charm, stable_revision_matrix)
            continue
        at_least_one_charm = True
        k8s_operator_bundle.set(charm, from_revision_matrix)

    if not k8s_operator_bundle.is_testable():
        log.info(f"k8s is missing a charm in channel={from_channel}. Skipping...")
        return ProcessState.PROCESS_UNCHANGED

    if not at_least_one_charm:
        log.info(f"Charm has no revisions in channel={from_channel}. Skipping...")
        return ProcessState.PROCESS_UNCHANGED

    try:
        state = ensure_track_state(
            from_channel, k8s_operator_bundle, dry_run, priority_generator
        )
        log.info(f"Track {track} is in state: {state}")

        if state.empty:
            log.info("Track state is empty and indicative of a CI failure. Skipping...")
            return ProcessState.PROCESS_CI_FAILED
        elif state.succeeded:
            log.info(f"Release run for {track} succeeded. Promoting charm revisions...")
            if not dry_run:
                for charm in bundle_charms:
                    charmhub.promote_charm(charm, from_channel, to_channel)
            return ProcessState.PROCESS_SUCCESS
        elif state.in_progress:
            log.info(f"Release run for {track} is still in progress. No action needed.")
            return ProcessState.PROCESS_IN_PROGRESS
        elif state.failed:
            log.info(f"Release run for {track} failed. Manual intervention required.")
            return ProcessState.PROCESS_FAILED
        else:
            log.info(f"Unknown state for {track}. Skipping...")
            return ProcessState.PROCESS_CI_FAILED
    except sqa.SQAFailure:
        log.exception(f"process track {track} failed because of the SQA")
        return ProcessState.PROCESS_CI_FAILED
    except charmhub.CharmcraftFailure:
        log.exception(f"process track {track} failed because of the Charmcraft")
        return ProcessState.PROCESS_CI_FAILED
    except sqa.InvalidSQAInput:
        log.exception(f"process track {track} failed because of revision could not be extracted from version")
        return ProcessState.PROCESS_CI_FAILED


def main():
    parser = argparse.ArgumentParser(
        description="Automate k8s-operator charm release process."
    )
    parser.add_argument(
        "--dry-run", action="store_true", required=False, help="Dry run the charm release process"
    )
    parser.add_argument(
        "--charms", nargs="+", default=["k8s", "k8s-worker"], help="List of charms used in k8s-operator"
    )
    parser.add_argument(
        "--from-risk", default="candidate",
        help="Source risk level for the charm release process (default: candidate)"
    )
    parser.add_argument(
        "--to-risk", default="stable",
        help="Target risk level for the charm release process (default: stable)"
    )
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        "--supported-tracks", nargs="+", default=[], help="List of tracks to check for"
    )
    group.add_argument("--after", nargs=1, default="1.32", help="Least supported track")

    args = parser.parse_args()

    if args.supported_tracks:
        tracks = args.supported_tracks
    else:
        log.info(f"Getting all Kubernetes releases after {args.after} inclusive.")
        tracks = k8s.get_all_releases_after(args.after)

    if not tracks:
        log.info("No tracks found for charm release process. Skipping...")
        return

    log.info(f"Starting the charms {args.charms} release process for: {tracks}")

    results = {}
    priority_generator = sqa.PriorityGenerator(initial=5)
    for track in tracks:
        process_state = process_track(track, priority_generator, args)
        if process_state in [
            ProcessState.PROCESS_IN_PROGRESS,
            ProcessState.PROCESS_UNCHANGED,
        ]:
            continue
        results[track] = str(process_state)

    with open("results.txt", "w") as f:
        for key, value in results.items():
            f.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
