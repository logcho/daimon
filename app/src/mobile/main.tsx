import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import "../index.css";
import { Pair } from "./Pair";
import { Remote } from "./Remote";
import { storedToken } from "./transport";

function App() {
  const [paired, setPaired] = useState(() => storedToken() !== null);
  return paired
    ? <Remote onUnauthorized={() => setPaired(false)} />
    : <Pair onPaired={() => setPaired(true)} />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
