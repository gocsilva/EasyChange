"""Pinned Kubernetes namespace cannot be overridden via compact -nVALUE."""
from __future__ import annotations

import pytest

from easychange.core.ubuntu_service import UbuntuError, UbuntuService, UbuntuSession


@pytest.mark.parametrize("args,expected", [
    (["delete","pod","victim","-nprod"],"prod"),
    (["delete","pod","victim","-n=prod"],"prod"),
    (["delete","pod","victim","--namespace=prod"],"prod"),
    (["delete","pod","victim","-n","prod"],"prod"),
])
def test_namespace_override_detection_recognizes_all_supported_forms(args,expected):
    assert UbuntuService._requested_namespace(args)==expected


def test_compact_namespace_override_blocked_before_remote_job():
    service=UbuntuService(object())
    service._sessions["hml"]=UbuntuSession(
        session_id="hml",distro="Ubuntu",cwd="/tmp",
        namespace="hml",context="aks-hml",
    )
    with pytest.raises(UbuntuError,match="KUBECTL_MUTATION_NAMESPACE_OVERRIDE_FORBIDDEN"):
        service.kubectl(
            "hml",["delete","pod","victim","-nprod"],
            job_id="JNO",allow_mutation=True,confirm_context="aks-hml",
        )


def test_compact_pinned_namespace_is_not_shadowed_by_second_namespace_flag():
    service=UbuntuService(object())
    session=UbuntuSession(
        session_id="hml",distro="Ubuntu",cwd="/tmp",
        namespace="hml",context="aks-hml",
    )
    args,namespace=service._effective_kubectl_args(
        session,["get","pods","-nhml"],
    )
    assert namespace=="hml"
    assert args.count("--namespace")==0
    assert args[-1]=="-nhml"


def test_conflicting_repeated_namespace_flags_rejected_before_remote_job():
    service=UbuntuService(object())
    service._sessions["hml"]=UbuntuSession(
        session_id="hml",distro="Ubuntu",cwd="/tmp",
        namespace="hml",context="aks-hml",
    )
    with pytest.raises(UbuntuError,match="KUBECTL_NAMESPACE_OVERRIDE_CONFLICT"):
        service.kubectl(
            "hml",["delete","pod","victim","-n","hml","-nprod"],
            job_id="JNO2",allow_mutation=True,confirm_context="aks-hml",
        )
