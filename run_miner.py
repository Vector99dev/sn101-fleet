"""SN101 miner entry point with optional IP-hiding.

When SN101_HIDE_AXON_IP=1 (default), this wrapper monkey-patches the
tag101 ChainRuntime so the on-chain axon entry published for this miner
is ``0.0.0.0:<real_port>`` — the IP is hidden, the port is the real one.
Validators discover the real endpoint via tag101's signed private-axon
announcement to the task server (which the miner does at startup anyway).

NOTE: the chain rejects port=0 (custom error 13 = Invalid Transaction),
so we can't publish a fully-null entry once a real IP has been published
before. Publishing 0.0.0.0 with the real port is the best chain-accepted
hide — see metagraph UIDs 7/8/9 for prior art that the chain accepts.

What the user sees on the metagraph for this hotkey:
    IP=0.0.0.0  PORT=<real_port>
What validators see (via task server's private-axon table):
    IP=<real>   PORT=<real>

Why this matters:
    Anti-DDoS / anti-fingerprint. Stops random internet scanners from
    discovering the miner's IP through public metagraph queries. The
    port alone isn't useful to an attacker — they'd have to also know
    your IP, which is no longer exposed.

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
        """Publish 0.0.0.0:<real_port> to chain; keep real port for local listener."""
        import bittensor as bt  # imported lazily

        # Stash real external IP/port (may be None — bittensor auto-detects).
        real_ip = getattr(axon, "external_ip", None)
        real_port = getattr(axon, "external_port", None)

        # We want to publish IP=0.0.0.0 but a NON-ZERO port (chain rejects 0).
        # Use the real bind port. The IP-hide is what matters for anti-DDoS;
        # leaking the port number doesn't help an attacker without the IP.
        hide_port = real_port or getattr(axon, "port", 0) or 1
        if not hide_port:
            hide_port = 1  # last-resort fallback to keep chain happy

        try:
            axon.external_ip = "0.0.0.0"
            axon.external_port = int(hide_port)
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
                bt.logging.warning(
                    f"[run_miner] serve_axon(0.0.0.0:{hide_port}) rejected: "
                    f"{exc}; skipping chain publish to keep IP hidden."
                )
                import types
                return types.SimpleNamespace(success=True, message="skip")
            if not getattr(response, "success", False):
                bt.logging.warning(
                    f"[run_miner] serve_axon(0.0.0.0:{hide_port}) returned: "
                    f"{getattr(response, 'message', response)}"
                )
            else:
                bt.logging.info(
                    f"[run_miner] published 0.0.0.0:{hide_port} to chain — "
                    "IP hidden"
                )
            return response
        finally:
            # Restore real external IP/port so the local axon still binds
            # and serves on the real port for validator queries.
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
