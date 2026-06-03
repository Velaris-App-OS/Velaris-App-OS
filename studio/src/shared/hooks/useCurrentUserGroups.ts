import { useState, useEffect } from "react";
import { useAuth } from "@/auth/AuthContext";
import { getMyDirectoryEntry } from "@shared/api/client";

const _cache = new Map<string, string[]>();

export function useCurrentUserGroups(): string[] {
  const { user } = useAuth();
  const userId = user?.user_id ?? "";
  const [groups, setGroups] = useState<string[]>(_cache.get(userId) ?? []);

  useEffect(() => {
    if (!userId) return;
    if (_cache.has(userId)) { setGroups(_cache.get(userId)!); return; }
    getMyDirectoryEntry(userId)
      .then((entry) => {
        const g: string[] = entry.access_group_ids ?? [];
        _cache.set(userId, g);
        setGroups(g);
      })
      .catch(() => {});
  }, [userId]);

  return groups;
}
