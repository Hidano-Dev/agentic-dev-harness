#!/usr/bin/env python3
"""Linear 側の前提を検証し、ワーカー用ラベルを用意する。

- チーム名が存在すること(無ければ候補一覧を出して失敗 → config.json に
  誤ったチーム名が入るのを防ぐ)
- プロジェクト名を指定した場合、そのチームに存在すること
- `needs-human` / needs_local ラベルが(ワークスペース共通またはそのチームに)
  存在すること。無ければチームラベルとして作成する

認証: 環境変数 LINEAR_API_KEY(個人 API キー。`Authorization: <key>`)。
ワークフローでは secret が未設定なら本スクリプトごとスキップされる。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.linear.app/graphql"

LABELS = {
    # name は引数で差し替わる(needs_local はリポジトリごとに命名してよい)
    "needs_human": {
        "description": "人間の判断が必要。自律ワーカー(linear-worker)は着手しない",
        "color": "#eb5757",
    },
    "needs_local": {
        "description": "ローカル環境・実機が必要。自律ワーカーはスキップしてユーザーへ通知する",
        "color": "#f2c94c",
    },
}


class LinearError(RuntimeError):
    pass


def gql(query: str, variables: dict | None = None) -> dict:
    key = os.environ.get("LINEAR_API_KEY", "").strip()
    if not key:
        raise LinearError("LINEAR_API_KEY が設定されていない")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise LinearError(f"Linear API HTTP {e.code}: {e.read().decode(errors='replace')[:500]}") from e
    if payload.get("errors"):
        raise LinearError("Linear API error: " + json.dumps(payload["errors"], ensure_ascii=False)[:1000])
    return payload["data"]


def find_team(name: str) -> dict:
    data = gql(
        "query($name: String!) { teams(filter: { name: { eq: $name } }) { nodes { id name key } } }",
        {"name": name},
    )
    nodes = data["teams"]["nodes"]
    if len(nodes) == 1:
        return nodes[0]
    if not nodes:
        all_teams = gql("query { teams(first: 100) { nodes { name key } } }")["teams"]["nodes"]
        names = ", ".join(f"{t['name']} ({t['key']})" for t in all_teams) or "(なし)"
        raise LinearError(f"チーム '{name}' が見つからない。存在するチーム: {names}")
    raise LinearError(f"チーム名 '{name}' が複数ヒットした({len(nodes)} 件)。名前を一意にすること")


def check_project(team_id: str, project: str) -> None:
    data = gql(
        "query($id: String!, $name: String!) {"
        "  team(id: $id) { projects(filter: { name: { eq: $name } }) { nodes { id name } } }"
        "}",
        {"id": team_id, "name": project},
    )
    if not data["team"]["projects"]["nodes"]:
        avail = gql(
            "query($id: String!) { team(id: $id) { projects(first: 100) { nodes { name } } } }",
            {"id": team_id},
        )["team"]["projects"]["nodes"]
        names = ", ".join(p["name"] for p in avail) or "(なし)"
        raise LinearError(f"プロジェクト '{project}' がチームに無い。存在するプロジェクト: {names}")


def ensure_label(team_id: str, name: str, description: str, color: str) -> str:
    """戻り値: 'exists' | 'created'"""
    data = gql(
        "query($name: String!) { issueLabels(filter: { name: { eq: $name } }) { nodes { id name team { id } } } }",
        {"name": name},
    )
    for node in data["issueLabels"]["nodes"]:
        team = node.get("team")
        if team is None or team["id"] == team_id:
            return "exists"
    payload = gql(
        "mutation($input: IssueLabelCreateInput!) {"
        "  issueLabelCreate(input: $input) { success issueLabel { id name } }"
        "}",
        {"input": {"name": name, "teamId": team_id, "description": description, "color": color}},
    )["issueLabelCreate"]
    # トップレベルの GraphQL error なしで success=false が返る(権限・入力検証で拒否)場合がある
    if not payload.get("success") or not (payload.get("issueLabel") or {}).get("id"):
        raise LinearError(f"ラベル '{name}' の作成が成功しなかった: {json.dumps(payload, ensure_ascii=False)}")
    return "created"


def write_outputs(outputs: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in outputs.items():
            f.write(f"{k}={v}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--team", required=True)
    ap.add_argument("--project", default="")
    ap.add_argument("--needs-human", default="needs-human")
    ap.add_argument("--needs-local", default="needs-local")
    a = ap.parse_args(argv)

    try:
        team = find_team(a.team)
        print(f"チーム: {team['name']} (key {team['key']})")
        if a.project.strip():
            check_project(team["id"], a.project.strip())
            print(f"プロジェクト: {a.project.strip()} (存在確認 OK)")
        results = {}
        for kind, label_name in (("needs_human", a.needs_human), ("needs_local", a.needs_local)):
            meta = LABELS[kind]
            results[label_name] = ensure_label(team["id"], label_name, meta["description"], meta["color"])
            print(f"ラベル {label_name}: {results[label_name]}")
    except LinearError as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1

    write_outputs(
        {
            "team_key": team["key"],
            "labels": json.dumps(results, ensure_ascii=False),
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
