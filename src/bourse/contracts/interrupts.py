"""Closed INPUT_REQUIRED schemas (ADR-0040). Agent free text is never accepted."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class ConfirmColumns(BaseModel):
    """Ask the broker to confirm a subset of the dataset header."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["confirm_columns"] = "confirm_columns"
    columns: list[str]


class ConfirmUnits(BaseModel):
    """Ask the broker to pick a unit from the closed unit table for one column."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["confirm_units"] = "confirm_units"
    column: str
    candidates: list[str]


InputRequest = Annotated[ConfirmColumns | ConfirmUnits, Field(discriminator="type")]
INPUT_ADAPTER: TypeAdapter[ConfirmColumns | ConfirmUnits] = TypeAdapter(InputRequest)
