import { useEffect, useRef, useState } from "react";

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
  const trigger = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef(false);

  useEffect(() => {
    if (confirming) cancel.current?.focus();
    else if (returnFocus.current) {
      returnFocus.current = false;
      trigger.current?.focus();
    }
  }, [confirming]);

  if (!confirming) {
    return (
      <button ref={trigger} type="button" disabled={disabled} onClick={() => setConfirming(true)}>
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
      <button
        ref={cancel}
        type="button"
        disabled={disabled}
        onClick={() => {
          returnFocus.current = true;
          setConfirming(false);
        }}
      >
        Cancel
      </button>
    </span>
  );
}
