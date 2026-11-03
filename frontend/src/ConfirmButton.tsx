import { useState } from "react";

/** A two-step button: the first click asks, the second confirms. */
export function ConfirmButton({
  label,
  confirmLabel,
  onConfirm,
  disabled,
}: {
  label: string;
  confirmLabel: string;
  onConfirm: () => void;
  disabled?: boolean;
}) {
  const [confirming, setConfirming] = useState(false);
  if (!confirming) {
    return (
      <button type="button" disabled={disabled} onClick={() => setConfirming(true)}>
        {label}
      </button>
    );
  }
  return (
    <span role="group" aria-label={confirmLabel} className="actions">
      <button
        type="button"
        disabled={disabled}
        onClick={() => {
          setConfirming(false);
          onConfirm();
        }}
      >
        {confirmLabel}
      </button>
      <button type="button" disabled={disabled} onClick={() => setConfirming(false)}>
        Cancel
      </button>
    </span>
  );
}
