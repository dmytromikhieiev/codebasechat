import { useEffect, useState } from "react";
import { getMe } from "./api";
import type { Me } from "./types";
import { LoginPage } from "./components/LoginPage";
import { Dashboard } from "./components/Dashboard";

export default function App() {
  const [me, setMe] = useState<Me | null | "loading">("loading");

  useEffect(() => {
    getMe()
      .then(setMe)
      .catch(() => setMe(null));
  }, []);

  if (me === "loading") {
    return <div className="flex h-screen items-center justify-center text-gray-500">Загрузка…</div>;
  }

  if (me === null) {
    return <LoginPage />;
  }

  return <Dashboard me={me} />;
}
