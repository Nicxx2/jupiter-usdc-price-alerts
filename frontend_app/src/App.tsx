import AlertsDashboard from "@/pages/AlertsDashboard";
import { useEffect, useState } from "react";

function ThemeToggle() {
  const [isDark, setIsDark] = useState<boolean>(document.documentElement.classList.contains("dark"));

  useEffect(() => {
    const onChange = () => setIsDark(document.documentElement.classList.contains("dark"));
    const observer = new MutationObserver(onChange);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);

  const toggle = () => {
    const root = document.documentElement;
    const next = !isDark;
    if (next) root.classList.add("dark"); else root.classList.remove("dark");
    try { localStorage.setItem("theme", next ? "dark" : "light"); } catch {}
    setIsDark(next);
  };

  return (
    <button
      onClick={toggle}
      aria-label="Toggle theme"
      className="fixed top-3 right-3 z-50 rounded-full border border-gray-300 bg-white p-2 shadow hover:bg-gray-50 dark:border-gray-600 dark:bg-gray-800 dark:hover:bg-gray-700"
      title={isDark ? "Switch to light mode" : "Switch to dark mode"}
   >
      {/* Sun/Moon icon */}
      <span className="block h-5 w-5">
        {isDark ? (
          // Sun
          <svg viewBox="0 0 24 24" fill="currentColor" className="text-yellow-400">
            <path d="M6.76 4.84l-1.8-1.79-1.41 1.41 1.79 1.8 1.42-1.42zm10.48 0l1.79-1.79 1.41 1.41-1.79 1.8-1.41-1.42zM12 4V1h-0v3h0zm0 19v-3h0v3h0zM4 12H1v0h3v0zm19 0h-3v0h3v0zM6.76 19.16l-1.8 1.79 1.41 1.41 1.8-1.79-1.41-1.41zm10.48 0l1.41 1.41 1.79-1.79-1.41-1.41-1.79 1.79zM12 7a5 5 0 100 10 5 5 0 000-10z"/>
          </svg>
        ) : (
          // Moon
          <svg viewBox="0 0 24 24" fill="currentColor" className="text-gray-700">
            <path d="M21.64 13a9 9 0 11-10.63-10.6 7 7 0 1010.63 10.6z"/>
          </svg>
        )}
      </span>
    </button>
  );
}

export default function App() {
  return (
    <main className="min-h-screen bg-gray-100 text-gray-900 dark:bg-gray-900 dark:text-gray-100">
      <ThemeToggle />
      <AlertsDashboard />
    </main>
  );
}
