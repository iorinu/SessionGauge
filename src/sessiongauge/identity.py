from sessiongauge.domain import Match, Pane


def match_panes(panes, databases: dict[str, set[str]]) -> list[Match]:
    parsed = [
        pane
        if isinstance(pane, Pane)
        else Pane(
            str(pane.get("endpoint", "local-herdr")),
            str(pane["pane_id"]),
            pane.get("terminal_id"),
            pane.get("agent"),
            pane.get("agent_status"),
            pane.get("cwd"),
            pane.get("title"),
            pane.get("session_id"),
        )
        for pane in panes
    ]
    uses: dict[str, int] = {}
    for pane in parsed:
        if pane.session_id:
            uses[pane.session_id] = uses.get(pane.session_id, 0) + 1
    matches = []
    for pane in parsed:
        sid = pane.session_id
        owners = [name for name, ids in databases.items() if sid and sid in ids]
        if sid and uses[sid] > 1:
            matches.append(Match(pane, None, None, "conflict"))
        elif len(owners) == 1:
            matches.append(Match(pane, owners[0], sid, "matched"))
        elif len(owners) > 1:
            matches.append(Match(pane, None, None, "conflict"))
        else:
            matches.append(Match(pane, None, None, "unlinked"))
    return matches


def lineage(session_id: str, parents: dict[str, str | None]) -> tuple[str, ...]:
    chain, seen, current = [], set(), session_id
    while current and current not in seen:
        seen.add(current)
        chain.append(current)
        current = parents.get(current)
    return tuple(chain)
