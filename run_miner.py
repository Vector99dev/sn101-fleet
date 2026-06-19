"""SN101 miner entry point with optional IP-hiding.

When SN101_HIDE_AXON_IP=1 (default), this wrapper monkey-patches the
tag101 ChainRuntime so the on-chain axon entry published for this miner
is ``0.0.0.0:0`` instead of the real IP:port. Validators discover the
real endpoint via tag101's signed private-axon announcement to the task
server (which the miner does at startup anyway).

What the user sees on the metagraph for this hotkey:
    IP=0.0.0.0  PORT=0
What validators see (via task server):
    IP=<real>   PORT=<real>

Why this matters:
    Anti-DDoS / anti-fingerprint. Stops random internet scanners from
    discovering the miner's IP through public metagraph queries. Only
    SN101 validators (who can read the private-axon table from the task
    server) know how to reach the miner.

Disable:
    SN101_HIDE_AXON_IP=0 (publishes the real IP:port like vanilla tag101)
"""

from __future__ import annotations

import os
import sys


def _install_hide_patch() -> None:
    """Patch tag101.chain.runtime.ChainRuntime.serve_axon BEFORE miner import."""
    from tag101.chain import runtime as _rt

    _orig_serve = _rt.ChainRuntime.serve_axon

    def _hidden_serve_axon(self, axon):
        """Publish 0.0.0.0:0 to chain; keep real port for the local listener."""
        import bittensor as bt  # imported lazily

        # Stash real external IP/port (may be None — bittensor auto-detects).
        real_ip = getattr(axon, "external_ip", None)
        real_port = getattr(axon, "external_port", None)

        try:
            axon.external_ip = "0.0.0.0"
            axon.external_port = 0
            try:
                response = self.subtensor.serve_axon(
                    netuid=self.config.netuid,
                    axon=axon,
                    wait_for_inclusion=True,
                    wait_for_finalization=False,
                    wait_for_revealed_execution=False,
                    period=None,
                )
            except Exception as exc:
                # Some bittensor versions reject port=0 at AxonInfo
                # validation. In that case, just skip the chain publish
                # entirely (best-effort hide). The hotkey's existing chain
                # entry will persist for now; new hotkeys will stay at
                # the default 0.0.0.0:0 since they never get a real entry.
                bt.logging.warning(
                    f"[run_miner] serve_axon(0.0.0.0:0) rejected: {exc}; "
                    f"skipping chain publish to keep IP hidden."
                )
                import types
                return types.SimpleNamespace(success=True, message="skip")
            if not getattr(response, "success", False):
                bt.logging.warning(
                    f"[run_miner] serve_axon(0.0.0.0:0) returned: "
                    f"{getattr(response, 'message', response)}"
                )
            else:
                bt.logging.info(
                    "[run_miner] published 0.0.0.0:0 to chain — IP hidden"
                )
            return response
        finally:
            # Restore real external IP/port so the local axon still binds
            # and serves the real port.
            if real_ip is not None:
                axon.external_ip = real_ip
            if real_port is not None:
                axon.external_port = real_port

    _rt.ChainRuntime.serve_axon = _hidden_serve_axon


def main() -> None:
    if os.environ.get("SN101_HIDE_AXON_IP", "1") == "1":
        _install_hide_patch()

    # Now import + run the standard tag101 miner. The patch is already in
    # effect because Python imports are cached and ChainRuntime hasn't been
    # instantiated yet.
    from tag101.miner import main as miner_main
    miner_main()


if __name__ == "__main__":
    main()
