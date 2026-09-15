from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from jsonschema.protocols import Validator
from pydantic import ValidationError

from gh_aw_router.contracts import (
    API_VERSION,
    ClassifyRequest,
    ErrorCode,
    ReasoningEffort,
    Role,
    RouteRequest,
    RoutingGoal,
    RoutingMode,
    RoutingModeRecommendation,
    RoutingObjective,
    TaskComplexity,
    TaskScope,
    TaskType,
)
from gh_aw_router.http import MAX_DETAIL_CHARS, create_app
from gh_aw_router.service import GhAwRouterService

OPENAPI_PATH = Path(__file__).resolve().parents[1] / "openapi.yaml"


def load_openapi() -> dict[str, Any]:
    return yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))


def test_committed_openapi_describes_the_closed_planning_contract() -> None:
    document = load_openapi()

    assert document["openapi"] == "3.1.0"
    assert document["info"]["version"] == API_VERSION
    assert set(document["paths"]) == {"/healthz", "/capabilities", "/classify", "/route"}
    schemas = document["components"]["schemas"]
    assert schemas["ClassifyRequest"]["properties"]["api_version"]["const"] == API_VERSION
    assert schemas["RouteRequest"]["properties"]["api_version"]["const"] == API_VERSION
    labels = schemas["Labels"]
    assert labels["additionalProperties"] is False
    assert labels["required"] == [
        "task_type",
        "scope",
        "task_complexity",
    ]
    assert labels["properties"]["task_type"]["enum"] == _values(TaskType)
    assert labels["properties"]["scope"]["enum"] == _values(TaskScope)
    assert schemas["TaskComplexity"]["enum"] == _values(TaskComplexity)
    assert schemas["RoutingGoal"]["enum"] == _values(RoutingGoal)
    assert schemas["RoutingMode"]["enum"] == _values(RoutingMode)
    assert schemas["PublishedRoutingMode"]["enum"] == ["economy", "balanced", "robust"]
    assert set(schemas["RoutingObjective"]["properties"]) == {"goal", "mode"}
    assert "labels" not in schemas["RouteRequest"]["properties"]
    assert schemas["RoutingModeRecommendation"]["enum"] == _values(RoutingModeRecommendation)
    assert schemas["ReasoningEffort"]["enum"] == _values(ReasoningEffort)
    assert schemas["Message"]["properties"]["role"]["enum"] == _values(Role)
    assert schemas["Error"]["properties"]["code"]["enum"] == _values(ErrorCode)
    assert schemas["Error"]["properties"]["detail"]["maxLength"] == MAX_DETAIL_CHARS


@pytest.mark.parametrize(
    ("payload", "valid"),
    [
        ({"goal": "cost", "mode": "balanced"}, True),
        ({"goal": "cost-speed", "mode": "balanced"}, True),
        ({"goal": "cost", "mode": "auto"}, True),
        ({"goal": "cost-speed", "mode": "auto"}, True),
        ({"goal": "cost-speed", "mode": "auto", "overrides": None}, False),
        ({"goal": "cost", "mode": "auto", "overrides": {"cost": 500}}, False),
        ({"goal": "cost-speed", "mode": "custom", "overrides": {"cost": 500}}, False),
        ({"goal": "cost", "mode": "custom"}, False),
        ({"goal": "cost", "mode": "balanced", "overrides": None}, False),
        ({"goal": "cost", "mode": "balanced", "overrides": {"cost": 1}}, False),
        ({"goal": "speed", "mode": "balanced"}, False),
    ],
)
def test_objective_shapes_match_runtime(payload: dict[str, Any], valid: bool) -> None:
    assert _validator("RoutingObjective").is_valid(payload) is valid
    if valid:
        RoutingObjective.model_validate_json(json.dumps(payload), strict=True)
    else:
        with pytest.raises(ValidationError):
            RoutingObjective.model_validate_json(json.dumps(payload), strict=True)


def test_documented_paths_match_registered_routes(service: GhAwRouterService) -> None:
    app = create_app(service)
    actual = {
        (route.path, method.lower())
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }
    expected = {
        (path, method)
        for path, operations in load_openapi()["paths"].items()
        for method in operations
    }
    assert actual == expected


def test_classifier_extension_and_route_reuse_the_classifier_output_schema() -> None:
    document = load_openapi()
    reference = document["x-gh-aw-router-classifier-output-schema"]
    assert reference == {"$ref": "#/components/schemas/ClassifierOutput"}
    schemas = document["components"]["schemas"]
    assert schemas["RouteRequest"]["properties"]["classification"]["anyOf"][0] == reference
    classifier = schemas["ClassifierOutput"]

    assert classifier["additionalProperties"] is False
    assert classifier["properties"]["labels"]["$ref"] == "#/components/schemas/Labels"
    assert classifier["properties"]["mode"]["$ref"] == (
        "#/components/schemas/RoutingModeRecommendation"
    )


def test_every_local_openapi_reference_resolves() -> None:
    document = load_openapi()
    references = set(_local_references(document))

    assert references
    for reference in references:
        _resolve_reference(document, reference)


def _validator(schema: str) -> Validator:
    document = load_openapi()
    return Draft202012Validator(
        {
            "$ref": f"#/components/schemas/{schema}",
            "components": document["components"],
        }
    )


@pytest.mark.parametrize("command", ["classify", "route"])
def test_http_examples_conform_to_committed_schemas(
    service: GhAwRouterService, command: str
) -> None:
    root = OPENAPI_PATH.parent
    payload = json.loads((root / "examples" / f"{command}-request.json").read_bytes())
    name = command.capitalize()
    _validator(f"{name}Request").validate(payload)
    response = TestClient(create_app(service)).post(f"/{command}", json=payload)
    assert response.status_code == 200
    _validator(f"{name}Response").validate(response.json())


@pytest.mark.parametrize("model", [ClassifyRequest, RouteRequest])
def test_nullable_request_fields_match_committed_schema(
    model: type[ClassifyRequest] | type[RouteRequest],
) -> None:
    payload: dict[str, Any] = {
        "api_version": API_VERSION,
        "repository": "acme/widgets",
        "task_id": "issue-123",
        "conversation": [],
        "models": [{"id": "plain", "model": "provider/plain", "effort": None}],
    }
    if model is RouteRequest:
        payload.update(
            {
                "objective": {"goal": "cost", "mode": "balanced"},
                "current_id": None,
                "classification": None,
            }
        )
        payload["models"][0]["context_window"] = None
    model.model_validate(payload)
    _validator(model.__name__).validate(payload)


@pytest.mark.parametrize("legacy_labels", ["omitted", "null", "object"])
@pytest.mark.parametrize(
    ("mode", "valid"),
    [
        ("economy", True),
        ("balanced", True),
        ("robust", True),
        ("unknown", True),
        ("auto", False),
        ("custom", False),
        (None, False),
    ],
)
def test_route_classification_shapes_match_runtime(
    planning_payload: Callable[[str], dict[str, Any]],
    legacy_labels: str,
    mode: str | None,
    valid: bool,
) -> None:
    payload = planning_payload("route")
    payload["objective"]["mode"] = "auto"
    labels = {"task_type": "fix", "scope": "local", "task_complexity": "easy"}
    payload["classification"] = {"labels": labels, "mode": mode}
    if legacy_labels != "omitted":
        payload["labels"] = labels if legacy_labels == "object" else None
    expected = valid and legacy_labels == "omitted"

    assert _validator("RouteRequest").is_valid(payload) is expected
    if expected:
        RouteRequest.model_validate_json(json.dumps(payload), strict=True)
    else:
        with pytest.raises(ValidationError):
            RouteRequest.model_validate_json(json.dumps(payload), strict=True)


@pytest.mark.parametrize("defect", ["labels", "mode", "extra"])
def test_invalid_classifier_output_is_rejected_by_runtime_and_document(
    planning_payload: Callable[[str], dict[str, Any]], defect: str
) -> None:
    payload = planning_payload("route")
    classification: dict[str, Any] = {
        "labels": {"task_type": "fix", "scope": "local", "task_complexity": "easy"},
        "mode": "balanced",
    }
    if defect == "extra":
        classification[defect] = True
    else:
        del classification[defect]
    payload["classification"] = classification

    assert not _validator("RouteRequest").is_valid(payload)
    with pytest.raises(ValidationError):
        RouteRequest.model_validate_json(json.dumps(payload), strict=True)


@pytest.mark.parametrize("model", [ClassifyRequest, RouteRequest])
@pytest.mark.parametrize("field", ["repository", "task_id"])
@pytest.mark.parametrize("value", [None, "", " ", 123])
def test_invalid_metadata_is_rejected_by_runtime_and_document(
    model: type[ClassifyRequest] | type[RouteRequest], field: str, value: object
) -> None:
    payload = {
        "api_version": API_VERSION,
        "repository": "acme/widgets",
        "task_id": "issue-123",
        "conversation": [],
        "models": [],
    }
    if model is RouteRequest:
        payload["objective"] = {"goal": "cost", "mode": "balanced"}
    del payload[field]
    validator = _validator(model.__name__)
    assert not validator.is_valid(payload)
    with pytest.raises(ValidationError):
        model.model_validate(payload)
    payload[field] = value
    assert not validator.is_valid(payload)
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_capabilities_and_errors_conform_to_committed_schema(service: GhAwRouterService) -> None:
    client = TestClient(create_app(service))
    _validator("ServiceCapabilities").validate(client.get("/capabilities").json())
    response = client.post("/route", json={})
    assert response.status_code == 422
    _validator("Error").validate(response.json())


def _local_references(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/"):
            yield reference
        for child in value.values():
            yield from _local_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _local_references(child)


def _resolve_reference(document: dict[str, Any], reference: str) -> object:
    value: object = document
    for component in reference.removeprefix("#/").split("/"):
        assert isinstance(value, dict), f"cannot resolve {reference}"
        value = value[component.replace("~1", "/").replace("~0", "~")]
    return value


def _values(enum: type[Any]) -> list[str]:
    return [item.value for item in enum]
