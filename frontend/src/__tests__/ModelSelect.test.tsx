import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { EnabledModelsContext, type EnabledModelInfo } from "../context/EnabledModelsContext";
import { ModelSelect } from "../components/ModelSelect";

function makeModel(id: string, supportsVision = false): EnabledModelInfo {
  return {
    id,
    name: `Name of ${id}`,
    supports_vision: supportsVision,
    verified: true,
    prompt_price_per_token: 0.000003,
    completion_price_per_token: 0.000015,
  };
}

function withModels(models: EnabledModelInfo[], ui: React.ReactElement): React.ReactElement {
  return (
    <EnabledModelsContext.Provider value={{ models, loading: false, refresh: async () => {} }}>
      {ui}
    </EnabledModelsContext.Provider>
  );
}

function optionIds(select: any): string[] {
  return select.querySelectorAll("option").map((o: any) => String(o.props.value));
}

const NO_MODELS_HINT = "No models enabled — open Settings > Models";
const NO_VISION_HINT = "No vision-capable model enabled — open Settings > Models";

describe("ModelSelect", () => {
  it("renders only enabled models", () => {
    const onChange = vi.fn();
    render(
      withModels(
        [makeModel("a/model"), makeModel("b/model", true)],
        <ModelSelect
          id="chat-model"
          label="Chat Model"
          value="a/model"
          onChange={onChange}
          testId="chat-model-select"
        />
      )
    );

    const select = screen.getByTestId("chat-model-select");
    expect(optionIds(select)).toEqual(["a/model", "b/model"]);
    expect(select.querySelectorAll("option")[0].textContent).toBe("Name of a/model");
    expect(screen.getByLabelText("Chat Model") === select).toBe(true);
    expect(select.getAttribute("id")).toBe("chat-model");
    expect(select.props.disabled).toBe(false);
    expect(screen.queryByText(NO_MODELS_HINT)).toBeNull();
    expect(onChange).toHaveBeenCalledTimes(0);
  });

  it("visionOnly filters non-vision models", () => {
    render(
      withModels(
        [makeModel("a/model"), makeModel("b/vision", true), makeModel("c/vision", true)],
        <ModelSelect
          id="vision"
          label="Vision Model"
          value="b/vision"
          onChange={vi.fn()}
          visionOnly
          testId="vision-select"
        />
      )
    );

    expect(optionIds(screen.getByTestId("vision-select"))).toEqual(["b/vision", "c/vision"]);
  });

  it("falls back to first option when value is not enabled", () => {
    const onChange = vi.fn();
    render(
      withModels(
        [makeModel("a/model"), makeModel("b/model")],
        <ModelSelect
          id="chat-model"
          label="Chat Model"
          value="removed/model"
          onChange={onChange}
        />
      )
    );

    expect(onChange).toHaveBeenCalledWith("a/model");
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("falls back to first option when value is empty", () => {
    const onChange = vi.fn();
    render(
      withModels(
        [makeModel("a/model")],
        <ModelSelect id="chat-model" label="Chat Model" value="" onChange={onChange} />
      )
    );

    expect(onChange).toHaveBeenCalledWith("a/model");
  });

  it("visionOnly falls back to first vision-capable model", () => {
    const onChange = vi.fn();
    render(
      withModels(
        [makeModel("a/text"), makeModel("b/vision", true)],
        <ModelSelect
          id="vision"
          label="Vision Model"
          value="a/text"
          onChange={onChange}
          visionOnly
        />
      )
    );

    expect(onChange).toHaveBeenCalledWith("b/vision");
  });

  it("reports user selection through onChange", () => {
    const onChange = vi.fn();
    render(
      withModels(
        [makeModel("a/model"), makeModel("b/model")],
        <ModelSelect
          id="chat-model"
          label="Chat Model"
          value="a/model"
          onChange={onChange}
          testId="chat-model-select"
        />
      )
    );

    fireEvent.change(screen.getByTestId("chat-model-select"), { target: { value: "b/model" } });

    expect(onChange).toHaveBeenCalledWith("b/model");
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("empty list shows hint and disables select", () => {
    const onChange = vi.fn();
    render(
      withModels(
        [],
        <ModelSelect
          id="chat-model"
          label="Chat Model"
          value=""
          onChange={onChange}
          testId="chat-model-select"
        />
      )
    );

    const select = screen.getByTestId("chat-model-select");
    expect(select.props.disabled).toBe(true);
    expect(optionIds(select)).toEqual([]);
    expect(Boolean(screen.getByText(NO_MODELS_HINT))).toBe(true);
    expect(onChange).toHaveBeenCalledTimes(0);
  });

  it("clears a stale value when no model is enabled any more", () => {
    const onChange = vi.fn();
    render(
      withModels(
        [],
        <ModelSelect id="chat-model" label="Chat Model" value="removed/model" onChange={onChange} />
      )
    );

    expect(onChange).toHaveBeenCalledWith("");
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("no vision model shows vision hint", () => {
    render(
      withModels(
        [makeModel("a/model")],
        <ModelSelect
          id="vision"
          label="Vision Model"
          value=""
          onChange={vi.fn()}
          visionOnly
          testId="vision-select"
        />
      )
    );

    expect(screen.getByTestId("vision-select").props.disabled).toBe(true);
    expect(Boolean(screen.getByText(NO_VISION_HINT))).toBe(true);
    expect(screen.queryByText(NO_MODELS_HINT)).toBeNull();
  });
});
