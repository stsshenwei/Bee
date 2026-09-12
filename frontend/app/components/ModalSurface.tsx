"use client";

import { type HTMLAttributes, useLayoutEffect, useRef } from "react";

const modalStack: HTMLElement[] = [];
const inertOwners = new Map<HTMLElement, { count: number; previous: boolean }>();
let scrollOwners = 0;
let previousOverflow = "";

type ModalSurfaceProps = HTMLAttributes<HTMLElement> & {
  onClose: () => void;
  dismissible?: boolean;
};

export function ModalSurface({ onClose, dismissible = true, children, ...props }: ModalSurfaceProps) {
  const ref = useRef<HTMLElement>(null);
  const closeRef = useRef({ onClose, dismissible });
  useLayoutEffect(() => { closeRef.current = { onClose, dismissible }; });

  useLayoutEffect(() => {
    const modal = ref.current!;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    modalStack.push(modal);
    if (scrollOwners++ === 0) {
      previousOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";
    }

    // Keep the surface in its theme scope while isolating the background.
    const isolated: HTMLElement[] = [];
    let branch: HTMLElement = modal;
    while (branch.parentElement && branch !== document.body) {
      for (const sibling of branch.parentElement.children) {
        if (!(sibling instanceof HTMLElement) || sibling === branch || sibling.hasAttribute("data-modal-backdrop")) continue;
        const owner = inertOwners.get(sibling) || { count: 0, previous: sibling.inert };
        owner.count += 1;
        inertOwners.set(sibling, owner);
        sibling.inert = true;
        isolated.push(sibling);
      }
      branch = branch.parentElement;
    }

    const candidates = () => Array.from(modal.querySelectorAll<HTMLElement>(
      'button, a[href], input, select, textarea, iframe, [tabindex], [contenteditable="true"]',
    )).filter((node) => node.tabIndex >= 0 && !node.matches(":disabled") && !node.closest("[inert]") && node.getClientRects().length && getComputedStyle(node).visibility !== "hidden");
    const focusFirst = () => (candidates()[0] || modal).focus({ preventScroll: true });
    const preferred = modal.querySelector<HTMLElement>("[data-autofocus]");
    if (preferred) preferred.focus({ preventScroll: true });
    else focusFirst();

    function onKeyDown(event: KeyboardEvent) {
      if (modalStack[modalStack.length - 1] !== modal) return;
      if (event.key === "Escape" && !event.isComposing) {
        event.preventDefault();
        event.stopPropagation();
        if (closeRef.current.dismissible) closeRef.current.onClose();
      } else if (event.key === "Tab") {
        const items = candidates();
        const index = items.indexOf(document.activeElement as HTMLElement);
        if (!items.length || index < 0 || (event.shiftKey ? index === 0 : index === items.length - 1)) {
          event.preventDefault();
          (event.shiftKey ? items[items.length - 1] || modal : items[0] || modal).focus();
        }
      }
    }
    function onFocus(event: FocusEvent) {
      if (modalStack[modalStack.length - 1] === modal && !modal.contains(event.target as Node)) focusFirst();
    }
    document.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("focusin", onFocus);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      document.removeEventListener("focusin", onFocus);
      modalStack.splice(modalStack.indexOf(modal), 1);
      for (const element of isolated) {
        const owner = inertOwners.get(element)!;
        if (--owner.count === 0) {
          element.inert = owner.previous;
          inertOwners.delete(element);
        }
      }
      if (--scrollOwners === 0) document.body.style.overflow = previousOverflow;
      if (opener?.isConnected && !opener.closest("[inert]")) opener.focus({ preventScroll: true });
    };
  }, []);

  return <section {...props} ref={ref} role="dialog" aria-modal="true" tabIndex={-1}>{children}</section>;
}
