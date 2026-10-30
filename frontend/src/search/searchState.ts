import { useEffect, useLayoutEffect, useMemo, useSyncExternalStore } from 'react';
import type { SearchClient } from '../bridge/types';
import { SearchController } from '../searchController';

export type SearchPort = Pick<SearchClient, 'search'>;
export type SearchPageMode = 'application' | 'widget' | 'onboarding';
export type RevealOutcome = { status: 'revealed' } | { status: 'unavailable' | 'failed'; message?: string };
export type RevealAction = (resultId: string) => Promise<RevealOutcome>;

export function useSearchState(client: SearchPort, scope: string[] | null = null, initialQuery = '') {
  const controller = useMemo(() => new SearchController(client, scope, initialQuery), [client]);
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  const scopeKey = JSON.stringify(scope);
  useLayoutEffect(() => controller.setScope(scope), [controller, scopeKey]);
  useEffect(() => () => controller.dispose(), [controller]);
  return {
    ...state,
    results: state.response?.results ?? [],
    interpretation: state.response?.query_interpretation ?? null,
    ambiguities: state.response?.ambiguities ?? [],
    loading: state.state === 'loading',
    setQuery: (query: string) => controller.edit(query),
    select: (id: string | null) => controller.select(id),
    chooseProject: (id: string | null) => controller.chooseProject(id, state.generation),
    submit: () => controller.submit(),
    revealCandidate: (id?: string | null) => controller.revealCandidate(id),
    controller,
  };
}
