"""固定注册的客户端页面语义；只证明呈现情况，不授予业务权限。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .knowledge import published_text

Page = Literal["welcome", "profile", "trips", "booking", "approvals"]
PAGE_ZONES = {
    "welcome": ("welcome.header", "welcome.intro"),
    "profile": ("profile.form",),
    "trips": ("trips.create", "trips.list", "booking.detail", "booking.payment"),
    "booking": ("booking.detail", "booking.pricing", "booking.payment"),
    "approvals": ("approvals.list",),
}
PAGE_CONTROLS = {
    "welcome": ("identity.login",), "profile": ("profile.bind",),
    "trips": ("trips.create", "trips.select", "nav.booking"),
    "booking": ("nav.booking", "booking.quote", "booking.lock", "booking.cancel_lock",
                "booking.pay", "booking.reconcile"),
    "approvals": ("approvals.approve", "approvals.reject"),
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Zone(Strict):
    id: str = Field(min_length=1, max_length=40)
    present: bool
    visible: bool

    @model_validator(mode="after")
    def consistent(self):
        if self.visible and not self.present:
            raise ValueError("observation_visibility_invalid")
        return self


class Control(Zone):
    enabled: bool | None

    @model_validator(mode="after")
    def availability(self):
        if ((not self.present and self.enabled is not None) or (self.present and self.enabled is None)
                or (not self.visible and self.enabled is True)):
            raise ValueError("observation_control_invalid")
        return self


class Semantics(Strict):
    schema_version: Literal["page-semantics-1"] = Field(alias="schema")
    selected_trip: bool
    zones: list[Zone] = Field(max_length=4)
    controls: list[Control] = Field(max_length=6)


class Observation(Strict):
    page: Page
    title: str = Field(min_length=1, max_length=100)
    items: list[str] = Field(max_length=8)
    product_version: Literal["v1"]
    semantics: Semantics | None = None

    @model_validator(mode="after")
    def checked(self):
        for value in [self.title, *self.items]:
            if len(value) > 160:
                raise ValueError("observation_too_long")
            published_text(value)
        if self.semantics:
            for rows, allowed in ((self.semantics.zones, PAGE_ZONES[self.page]),
                                  (self.semantics.controls, PAGE_CONTROLS[self.page])):
                ids = [row.id for row in rows]
                if len(ids) != len(set(ids)) or set(ids) - set(allowed):
                    raise ValueError("observation_element_unregistered")
        return self


def observation_payload(value):
    """旧观察保持原字段；enabled=null保留为不存在控件的明确状态。"""
    result = value.model_dump(mode="json", by_alias=True)
    if result["semantics"] is None:
        result.pop("semantics")
    return result
