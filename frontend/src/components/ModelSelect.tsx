import React, { useEffect } from "react";
import { useEnabledModels } from "../context/EnabledModelsContext";

export interface ModelSelectProps {
  id: string;
  label: string;
  value: string;
  onChange: (modelId: string) => void;
  visionOnly?: boolean;
  testId?: string;
}

const NO_MODELS_HINT = "No models enabled — open Settings > Models";
const NO_VISION_MODELS_HINT = "No vision-capable model enabled — open Settings > Models";

export function ModelSelect({
  id,
  label,
  value,
  onChange,
  visionOnly = false,
  testId,
}: ModelSelectProps): React.ReactElement {
  const { models } = useEnabledModels();
  const options = visionOnly ? models.filter((m) => m.supports_vision) : models;
  const firstOptionId = options[0]?.id;
  const valueIsOption = options.some((m) => m.id === value);

  useEffect(() => {
    if (valueIsOption) return;
    if (firstOptionId !== undefined) {
      onChange(firstOptionId);
    } else if (value !== "") {
      onChange("");
    }
  }, [value, firstOptionId, valueIsOption, onChange]);

  return (
    <>
      <label htmlFor={id}>{label}</label>
      <select
        id={id}
        aria-label={label}
        data-testid={testId}
        value={value}
        disabled={options.length === 0}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((m) => (
          <option key={m.id} value={m.id}>
            {m.name}
          </option>
        ))}
      </select>
      {options.length === 0 && (
        <span className="lemma-model-select-hint" role="note">
          {visionOnly ? NO_VISION_MODELS_HINT : NO_MODELS_HINT}
        </span>
      )}
    </>
  );
}

export default ModelSelect;
