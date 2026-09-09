import { useEffect, useState } from "react";
import { ApiError, listAvailableRepos, syncInstallations } from "../api";
import type { AvailableRepo, Repo } from "../types";

export function RepoPicker({
  connectedRepos,
  onSynced,
  onClose,
}: {
  connectedRepos: Repo[];
  onSynced: () => void;
  onClose: () => void;
}) {
  const [available, setAvailable] = useState<AvailableRepo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState<string | null>(null);
  const [justConnected, setJustConnected] = useState<Set<string>>(new Set());

  useEffect(() => {
    listAvailableRepos()
      .then(setAvailable)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Не удалось получить список репозиториев"));
  }, []);

  const connectedNames = new Set(connectedRepos.map((r) => r.repo_full_name));

  async function connect(repoFullName: string) {
    setConnecting(repoFullName);
    setError(null);
    try {
      await syncInstallations(repoFullName);
      setJustConnected((prev) => new Set(prev).add(repoFullName));
      onSynced();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Не удалось подключить репозиторий");
    } finally {
      setConnecting(null);
    }
  }

  return (
    <div className="mb-4 rounded-lg border border-gray-200 p-4">
      <div className="mb-2 flex items-center justify-between">
        <p className="text-sm font-medium text-gray-900">Доступные репозитории</p>
        <button onClick={onClose} className="text-sm text-gray-400 hover:text-gray-700">
          Закрыть
        </button>
      </div>

      {error && <p className="mb-2 text-sm text-red-600">{error}</p>}
      {available === null && !error && <p className="text-sm text-gray-500">Загрузка…</p>}
      {available !== null && available.length === 0 && (
        <p className="text-sm text-gray-500">Установка найдена, но репозиториев в ней нет.</p>
      )}

      <ul className="space-y-2">
        {available?.map((repo) => {
          const isConnected = connectedNames.has(repo.repo_full_name) || justConnected.has(repo.repo_full_name);
          return (
            <li key={repo.repo_full_name} className="flex items-center justify-between text-sm">
              <span className="text-gray-800">{repo.repo_full_name}</span>
              {isConnected ? (
                <span className="text-green-600">Подключён</span>
              ) : (
                <button
                  onClick={() => connect(repo.repo_full_name)}
                  disabled={connecting === repo.repo_full_name}
                  className="rounded-lg border border-gray-300 px-3 py-1 text-xs text-gray-700 transition hover:bg-gray-100 disabled:opacity-40"
                >
                  {connecting === repo.repo_full_name ? "Подключаем…" : "Подключить"}
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
