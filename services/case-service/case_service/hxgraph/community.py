"""HxGraph — Community detection using label propagation (numpy only, no extras).

Equivalent to graphify's community output: groups related nodes into numbered
communities. Label propagation converges quickly and works well on sparse graphs.
"""
from __future__ import annotations

import random
from collections import defaultdict


def detect_communities(
    node_ids: list[str],
    edges: list[tuple[str, str]],
    iterations: int = 10,
    seed: int = 42,
) -> dict[str, int]:
    """Label propagation community detection.

    Args:
        node_ids: list of node id strings
        edges: list of (from_id, to_id) tuples (undirected — both directions used)
        iterations: number of propagation rounds
        seed: random seed for tie-breaking

    Returns:
        dict mapping node_id → community_id (integer)
    """
    if not node_ids:
        return {}

    rng = random.Random(seed)

    # Build undirected adjacency list
    adj: dict[str, list[str]] = defaultdict(list)
    for a, b in edges:
        adj[a].append(b)
        adj[b].append(a)

    # Initialise: each node is its own community
    labels: dict[str, str] = {nid: nid for nid in node_ids}

    for _ in range(iterations):
        order = list(node_ids)
        rng.shuffle(order)
        changed = False
        for nid in order:
            neighbours = adj[nid]
            if not neighbours:
                continue
            # Count neighbour labels
            counts: dict[str, int] = defaultdict(int)
            for nb in neighbours:
                counts[labels[nb]] += 1
            max_count = max(counts.values())
            candidates = [lbl for lbl, cnt in counts.items() if cnt == max_count]
            new_label = rng.choice(candidates)
            if new_label != labels[nid]:
                labels[nid] = new_label
                changed = True
        if not changed:
            break

    # Remap label strings → stable integer IDs
    unique_labels = sorted(set(labels.values()))
    label_to_int = {lbl: i for i, lbl in enumerate(unique_labels)}
    return {nid: label_to_int[lbl] for nid, lbl in labels.items()}


def community_hubs(
    community_map: dict[str, int],
    degree: dict[str, int],
    top_n: int = 5,
) -> dict[int, list[str]]:
    """Return the top-N highest-degree nodes per community (the 'hub' nodes)."""
    communities: dict[int, list[str]] = defaultdict(list)
    for nid, cid in community_map.items():
        communities[cid].append(nid)

    hubs: dict[int, list[str]] = {}
    for cid, members in communities.items():
        sorted_members = sorted(members, key=lambda n: degree.get(n, 0), reverse=True)
        hubs[cid] = sorted_members[:top_n]
    return hubs
