"""冻结 promotion 的 Git push 目标并核验受保护默认分支身份。"""

from __future__ import annotations

from pathlib import Path

from release_models import ReleaseContractError, endpoint_sha256, run_git


def origin_push_endpoint(repo: Path) -> tuple[str, str]:
    """冻结唯一实际 push endpoint 及摘要，使校验、审批和写入使用同一目标。

    remote URL 可能携带受限 userinfo，计划只持久化 SHA-256，不输出原值。这里读取
    push URL 而不是 fetch URL，因为后续副作用明确执行 ``git push origin``。
    """

    endpoints = [
        line
        for line in run_git(repo, "remote", "get-url", "--push", "--all", "origin").splitlines()
        if line
    ]
    if len(endpoints) != 1:
        raise ReleaseContractError("origin must declare exactly one push endpoint")
    endpoint = endpoints[0]
    return endpoint, endpoint_sha256(endpoint)


def verify_push_default_branch(repo: Path, push_endpoint: str, expected_branch: str) -> None:
    """从实际写入 endpoint 读取默认分支，并绑定当前受审 source commit。

    平台环境变量只声明期望值，不构成保护证据；真正的远端 symref 与 HEAD OID
    必须同时匹配，避免任意同名本地分支消费 promotion approval。
    """

    remote_head = run_git(repo, "ls-remote", "--symref", push_endpoint, "HEAD")
    lines = [line.split() for line in remote_head.splitlines() if line.strip()]
    symref = next((parts for parts in lines if parts[0] == "ref:"), None)
    identity = next(
        (parts for parts in lines if parts[-1] == "HEAD" and parts[0] != "ref:"),
        None,
    )
    expected_ref = f"refs/heads/{expected_branch}"
    if symref is None or len(symref) != 3 or symref[1:] != [expected_ref, "HEAD"]:
        raise ReleaseContractError(
            "origin default branch does not match the declared protected default branch"
        )
    if identity is None or len(identity) != 2:
        raise ReleaseContractError("origin default branch identity is unavailable")
    if identity[0] != run_git(repo, "rev-parse", "HEAD"):
        raise ReleaseContractError(
            "origin default branch does not point to the reviewed source commit"
        )


__all__ = ["origin_push_endpoint", "verify_push_default_branch"]
