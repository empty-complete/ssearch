import { ResultList } from '../components/ResultList';
import type { ResultMetadata } from '../bridge/types';
import type { Ref } from 'react';

interface SearchResultsProps {
  results: ResultMetadata[];
  selectedId: string | null;
  query: string;
  loading: boolean;
  listRef: Ref<HTMLDivElement>;
  onSelect: (id: string | null) => void;
  onActivate: (id: string) => void;
}

export function SearchResults(props: SearchResultsProps) {
  return <ResultList results={props.results} selectedId={props.selectedId}
    query={props.query} disabled={props.loading} listRef={props.listRef}
    onSelect={props.onSelect} onReveal={props.onActivate} />;
}
