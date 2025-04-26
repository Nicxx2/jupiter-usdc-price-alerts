import { ReactNode } from "react";

export function Card({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-lg border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-800">
      {children}
    </div>
  );
}

export function CardContent({ children, className = "" }: { children: ReactNode, className?: string }) {
  return <div className={`p-4 ${className}`}>{children}</div>;
}
