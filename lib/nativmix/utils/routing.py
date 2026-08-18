"""
Smart Linker utility for NativMix.
Handles dynamic port discovery, clean link management, and robust routing chains.
"""

import json
import logging
import re
import subprocess
import time

logger = logging.getLogger(__name__)

# Maximum seconds to wait for any pw-link / pw-dump / pactl call.
# If PipeWire hangs, the call raises subprocess.TimeoutExpired instead of
# blocking the caller thread indefinitely.
_SUBPROCESS_TIMEOUT = 5

_pw_dump_cache: tuple[float, str] | None = None  # (timestamp, stdout)
_PW_DUMP_CACHE_TTL = 0.5  # seconds


def build_loopback_load_args(vsink_monitor: str, hw_sink: str) -> list[str]:
    """
    pactl load-module arguments for a NativMix V-Sink monitor → hardware sink loopback.

    Explicit ``sink=`` tells WirePlumber's Pulse adapter where playback belongs so it
    stops retrying ``No input node for loopback-*`` when ``dont-link=1`` is set.
    """
    return [
        "module-loopback",
        f"source={vsink_monitor}",
        f"sink={hw_sink}",
        "dont-link=1",
    ]


def loopback_module_targets_hardware(
    module_argument: str,
    vsink_name: str,
    hw_sink: str,
) -> bool:
    """Return True if an existing loopback module already routes to ``hw_sink``."""
    return (
        f"source={vsink_name}.monitor" in module_argument
        and f"sink={hw_sink}" in module_argument
    )


def invalidate_pw_dump_cache() -> None:
    """Drop cached pw-dump output after graph changes (loopback reload, etc.)."""
    global _pw_dump_cache
    _pw_dump_cache = None


def _get_pw_dump() -> str:
    """Run pw-dump and cache the result for 500 ms."""
    global _pw_dump_cache
    now = time.monotonic()
    if _pw_dump_cache and now - _pw_dump_cache[0] < _PW_DUMP_CACHE_TTL:
        return _pw_dump_cache[1]
    result = subprocess.run(
        ["pw-dump"], capture_output=True, text=True, check=True,
        timeout=_SUBPROCESS_TIMEOUT,
    )
    _pw_dump_cache = (now, result.stdout)
    return result.stdout


def resolve_loopback_node(pulse_module_id: str, retries: int = 5) -> str | None:
    """
    Return the PipeWire node.name for a loopback module identified by its
    PulseAudio module ID (the integer printed by pactl load-module).

    Uses pw-dump to read the live PipeWire graph and matches the node whose
    'pulse.module.id' property equals pulse_module_id.  Retries up to
    `retries` × 200 ms to absorb registration latency after a fresh load.

    Returns None if the node cannot be found.
    """
    for attempt in range(retries):
        try:
            nodes = json.loads(_get_pw_dump())
            for n in nodes:
                if n.get("type") != "PipeWire:Interface:Node":
                    continue
                props = n.get("info", {}).get("props", {})
                if str(props.get("pulse.module.id")) == pulse_module_id:
                    node_name = props.get("node.name")
                    logger.debug(
                        "SmartLinker: module %s → node '%s' (attempt %d)",
                        pulse_module_id, node_name, attempt + 1,
                    )
                    return node_name
        except Exception as e:
            logger.debug("SmartLinker: pw-dump lookup failed (attempt %d): %s", attempt + 1, e)

        if attempt < retries - 1:
            time.sleep(0.2)

    logger.warning(
        "SmartLinker: could not resolve PipeWire node for module ID %s after %d retries",
        pulse_module_id, retries,
    )
    return None


def find_ports(node_pattern: str, direction: str = "output", port_pattern: str | None = None) -> list[str]:
    """
    Find ports for a given node pattern and direction.
    If node_pattern looks like an integer, it's treated as a Pulse Module ID
    and we use pw-dump to find the corresponding PipeWire node.

    Node matching is case-insensitive and strips leading/trailing whitespace
    from the pw-link output so the caller does not have to worry about the
    exact capitalisation or formatting used by the installed PipeWire version.
    """
    target_node_name = node_pattern

    # If node_pattern is a Pulse Module ID (integer), resolve it via pw-dump
    if node_pattern.isdigit():
        resolved = resolve_loopback_node(node_pattern, retries=1)
        if resolved:
            target_node_name = resolved
            logger.debug("SmartLinker: Resolved module ID %s → node '%s'",
                         node_pattern, target_node_name)

    cmd = ["pw-link", "-o" if direction == "output" else "-i"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True,
                                timeout=_SUBPROCESS_TIMEOUT)
        all_ports = result.stdout.splitlines()

        matched_ports = []
        for port in all_ports:
            if ":" not in port:
                continue
            # Strip whitespace — pw-link indents port lines with leading spaces
            node, port_name = port.split(":", 1)
            node = node.strip()
            # Case-insensitive match so "Loopback-42" is found by "loopback.*42"
            if (target_node_name == node) or re.search(target_node_name, node, re.IGNORECASE):
                if port_pattern is None or re.search(port_pattern, port_name, re.IGNORECASE):
                    # Store the stripped version so pw-link can use it
                    matched_ports.append(f"{node}:{port_name}")

        return matched_ports
    except subprocess.TimeoutExpired:
        logger.warning("pw-link port listing timed out after %ds (node=%s)", _SUBPROCESS_TIMEOUT, node_pattern)
        return []
    except subprocess.CalledProcessError as e:
        logger.warning("Failed to find ports for node=%s, port=%s (%s): %s",
                       node_pattern, port_pattern, direction, e.stderr)
        return []


def _links_exist(source_pattern: str, target_pattern: str) -> bool:
    """
    Return True if at least one active pw-link link already connects a node
    matching source_pattern to a node matching target_pattern.
    Used to suppress false-positive 'No ports found' warnings when a link
    was established by PipeWire before our retry loop started.
    """
    try:
        result = subprocess.run(["pw-link", "-l"], capture_output=True, text=True, check=True,
                                timeout=_SUBPROCESS_TIMEOUT)
        for line in result.stdout.splitlines():
            if " -> " not in line:
                continue
            src, dst = line.split(" -> ", 1)
            src_node = src.split(":", 1)[0].strip() if ":" in src else src.strip()
            dst_node = dst.split(":", 1)[0].strip() if ":" in dst else dst.strip()
            if (re.search(source_pattern, src_node, re.IGNORECASE) and
                    re.search(target_pattern, dst_node, re.IGNORECASE)):
                return True
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass
    return False


def clean_links(source_node: str | None = None, target_node: str | None = None) -> None:
    """
    Remove all existing links between specific nodes or globally if requested.
    Uses 'pw-link -d' to ensure the state is clean.
    """
    try:
        result = subprocess.run(["pw-link", "-l"], capture_output=True, text=True, check=True,
                                timeout=_SUBPROCESS_TIMEOUT)

        # pw-link -l output format: 'SourceNode:SourcePort -> TargetNode:TargetPort'
        for line in result.stdout.splitlines():
            if " -> " not in line:
                continue

            src, dst = line.split(" -> ", 1)
            src_node = src.split(":", 1)[0].strip() if ":" in src else src.strip()
            dst_node = dst.split(":", 1)[0].strip() if ":" in dst else dst.strip()

            should_delete = False
            if source_node and target_node:
                if (re.search(source_node, src_node, re.IGNORECASE) and
                        re.search(target_node, dst_node, re.IGNORECASE)):
                    should_delete = True
            elif source_node:
                if re.search(source_node, src_node, re.IGNORECASE):
                    should_delete = True
            elif target_node:
                if re.search(target_node, dst_node, re.IGNORECASE):
                    should_delete = True

            if should_delete:
                logger.debug("SmartLinker: Deleting redundant link: %s -> %s", src, dst)
                try:
                    subprocess.run(["pw-link", "-d", src, dst], capture_output=True,
                                   timeout=_SUBPROCESS_TIMEOUT)
                except subprocess.TimeoutExpired:
                    logger.warning("pw-link -d timed out after %ds (%s -> %s)",
                                   _SUBPROCESS_TIMEOUT, src, dst)

    except subprocess.TimeoutExpired:
        logger.warning("pw-link -l timed out after %ds", _SUBPROCESS_TIMEOUT)
    except subprocess.CalledProcessError as e:
        logger.warning("Failed to clean links: %s", e.stderr)


def smart_link(source_pattern: str, target_pattern: str,
               source_dir: str = "output", target_dir: str = "input",
               source_port_pattern: str | None = None, target_port_pattern: str | None = None) -> bool:
    """
    Link source ports to target ports by index.
    Ensures independence of naming conventions (FL/FR vs AUX0/1).
    Includes a retry loop for PipeWire registration latency.

    If source_pattern is a plain integer it is treated as a PulseAudio module
    ID: find_ports resolves it to the actual PipeWire node name via pw-dump.

    If no ports are found after all retries, checks whether a link already
    exists via pw-link -l before emitting a WARNING (prevents false positives
    when PipeWire established the connection autonomously).
    """
    source_ports: list[str] = []
    target_ports: list[str] = []

    # 5 × 200 ms = 1 s.  The node is resolved via pw-dump so ports should
    # appear on the first or second attempt at most.
    _MAX_RETRIES = 5
    for attempt in range(_MAX_RETRIES):
        source_ports = find_ports(source_pattern, direction=source_dir, port_pattern=source_port_pattern)
        target_ports = find_ports(target_pattern, direction=target_dir, port_pattern=target_port_pattern)

        if source_ports and target_ports:
            break

        if attempt < 2:
            if not source_ports:
                logger.debug(
                    "SmartLinker: waiting for source ports, attempt %d/%d (node='%s')",
                    attempt + 1, _MAX_RETRIES, source_pattern,
                )
            if not target_ports:
                logger.debug(
                    "SmartLinker: waiting for target ports, attempt %d/%d (node='%s')",
                    attempt + 1, _MAX_RETRIES, target_pattern,
                )

        time.sleep(0.2)

    if not source_ports or not target_ports:
        if _links_exist(source_pattern, target_pattern):
            logger.debug(
                "SmartLinker: no ports matched but link '%s' → '%s' already active — skipping",
                source_pattern, target_pattern,
            )
            return True
        if not source_ports:
            logger.warning(
                "SmartLinker: No source ports found for node='%s', port='%s' after %d retries",
                source_pattern, source_port_pattern, _MAX_RETRIES,
            )
        else:
            logger.warning(
                "SmartLinker: No target ports found for node='%s', port='%s' after %d retries",
                target_pattern, target_port_pattern, _MAX_RETRIES,
            )
        return False

    # Sort to ensure consistent pairing by index
    source_ports.sort()
    target_ports.sort()

    # We pair by index. If counts don't match, we link what we can.
    link_count = min(len(source_ports), len(target_ports))
    success = False

    for i in range(link_count):
        src = source_ports[i]
        dst = target_ports[i]

        try:
            # pw-link fails silently if already linked, which is fine
            subprocess.run(["pw-link", src, dst], capture_output=True, check=True,
                           timeout=_SUBPROCESS_TIMEOUT)
            logger.debug("SmartLinker: Linked %s -> %s", src, dst)
            success = True
        except subprocess.TimeoutExpired:
            logger.warning("pw-link timed out after %ds (%s -> %s)", _SUBPROCESS_TIMEOUT, src, dst)
        except subprocess.CalledProcessError:
            # Often happens if already linked — not an error
            pass

    return success
