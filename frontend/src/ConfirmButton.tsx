import { useEffect, useRef, useState } from "react";

/** A two-step button: the first click asks, the second confirms. */
export function ConfirmButton({
  label,
  confirmLabel,
  onConfirm,
  successFocusId,
  disabled,
}: {
  label: string;
  confirmLabel: string;
  onConfirm: () => Promise<boolean>;
  successFocusId: string;
  disabled?: boolean;
}) {
  const [confirming, setConfirming] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const group = useRef<HTMLSpanElement>(null);
  const returnFocus = useRef(false);

  useEffect(() => {
    if (confirming) cancel.current?.focus();
  }, [confirming]);

  useEffect(() => {
    if (!confirming && !disabled && returnFocus.current) {
      returnFocus.current = false;
      trigger.current?.focus();
    }
  }, [confirming, disabled]);

  const confirm = async () => {
    // Keep focus on the confirmation while its buttons are disabled during the request.
    group.current?.focus();
    const succeeded = await onConfirm();
    if (group.current === document.activeElement) {
      const destination = succeeded ? document.getElementById(successFocusId) : null;
      if (destination) destination.focus();
      else returnFocus.current = true;
    }
    setConfirming(false);
  };

  if (!confirming) {
    return (
      <button ref={trigger} type="button" disabled={disabled} onClick={() => setConfirming(true)}>
        {label}
      </button>
    );
  }
  return (
    <span ref={group} role="group" aria-label={confirmLabel} tabIndex={-1} className="actions">
      <button
        type="button"
        disabled={disabled}
        onClick={() => void confirm()}
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
