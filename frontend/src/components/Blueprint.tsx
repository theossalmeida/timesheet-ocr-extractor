import type { ReactNode } from "react";

export function Blueprint({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`blueprint ${className}`}><i aria-hidden className="corner tl" /><i aria-hidden className="corner tr" /><i aria-hidden className="corner bl" /><i aria-hidden className="corner br" />{children}</div>;
}
