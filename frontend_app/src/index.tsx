import React from "react";
import ReactDOM from "react-dom/client";
import "./index.css";
import AlertsDashboard from "./AlertsDashboard";

const root = ReactDOM.createRoot(document.getElementById("root") as HTMLElement);
root.render(
  <React.StrictMode>
    <AlertsDashboard />
  </React.StrictMode>
);
