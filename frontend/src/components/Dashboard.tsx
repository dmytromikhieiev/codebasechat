import { useCallback, useEffect, useState } from "react";
import type { Me, Repo, RepoDetail } from "../types";
import { getRepo, installUrl, listRepos, logout, reindexRepo } from "../api";
import { ChatView } from "./ChatView";

const POLL_INTERVAL_MS = 3000;

const STATUS_LABELS: Record<Repo["status"], string> = {
  pending_first_index: "Ожидает индексации",
  indexing: "Индексируется…",
  ready: "Готов",
  failed: "Ошибка индексации",
};

const STATUS_COLORS: Record<Repo["status"], string> = {
  pending_first_index: "text-gray-500",
  indexing: "text-blue-600",
  ready: "text-green-600",
  failed: "text-red-600",
};

export function Dashboard({ me }: { me: Me }) {
  const [repos, setRepos] = useState<Repo[]>([]);
  const [selectedRepoId, setSelectedRepoId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(() => {
    listRepos()
      .then(setRepos)
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    const hasIndexing = repos.some((r) => r.status === "indexing" || r.status === "pending_first_index");
    if (!hasIndexing) return;
    const timer = setInterval(refresh, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [repos, refresh]);

  const selectedRepo = repos.find((r) => r.id === selectedRepoId) ?? null;

  if (selectedRepo && selectedRepo.status === "ready") {
    return <ChatView repo={selectedRepo} onBack={() => setSelectedRepoId(null)} />;
  }

  return (
    <div className="mx-auto max-w-2xl p-6">
      <header className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-gray-900">rag-codebase-chat</h1>
          <p className="text-sm text-gray-500">{me.github_login ?? me.email}</p>
        </div>
        <div className="flex gap-2">
          <a
            href={installUrl()}
            className="rounded-lg bg-gray-900 px-4 py-2 text-sm text-white transition hover:bg-gray-700"
          >
            + Подключить репозиторий
          </a>
          <button
            onClick={() => logout().then(() => window.location.reload())}
            className="rounded-lg border border-gray-300 px-4 py-2 text-sm text-gray-700 transition hover:bg-gray-100"
          >
            Выйти
          </button>
        </div>
      </header>

      {loading && <p className="text-gray-500">Загрузка…</p>}
      {!loading && repos.length === 0 && (
        <p className="text-gray-500">Нет подключённых репозиториев — нажмите «Подключить репозиторий».</p>
      )}

      <ul className="space-y-3">
        {repos.map((repo) => (
          <RepoRow
            key={repo.id}
            repo={repo}
            onOpen={() => setSelectedRepoId(repo.id)}
            onReindex={() => reindexRepo(repo.id).then(refresh)}
          />
        ))}
      </ul>
    </div>
  );
}

function RepoRow({ repo, onOpen, onReindex }: { repo: Repo; onOpen: () => void; onReindex: () => void }) {
  const [detail, setDetail] = useState<RepoDetail | null>(null);

  useEffect(() => {
    if (repo.status !== "indexing") {
      setDetail(null);
      return;
    }
    let cancelled = false;
    const poll = () => {
      getRepo(repo.id).then((d) => {
        if (!cancelled) setDetail(d);
      });
    };
    poll();
    const timer = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [repo.id, repo.status]);

  return (
    <li className="flex items-center justify-between rounded-lg border border-gray-200 p-4">
      <div>
        <p className="font-medium text-gray-900">{repo.repo_full_name}</p>
        <p className={`text-sm ${STATUS_COLORS[repo.status]}`}>{STATUS_LABELS[repo.status]}</p>
        {repo.status === "indexing" && detail?.files_total ? (
          <p className="text-xs text-gray-400">
            {detail.files_done ?? 0}/{detail.files_total} файлов
          </p>
        ) : null}
      </div>
      <div className="flex gap-2">
        {repo.status === "ready" && (
          <button
            onClick={onOpen}
            className="rounded-lg bg-gray-900 px-3 py-1.5 text-sm text-white transition hover:bg-gray-700"
          >
            Чат
          </button>
        )}
        {repo.status !== "indexing" && (
          <button
            onClick={onReindex}
            className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm text-gray-700 transition hover:bg-gray-100"
          >
            Переиндексировать
          </button>
        )}
      </div>
    </li>
  );
}
