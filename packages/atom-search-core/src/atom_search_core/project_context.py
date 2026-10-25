"""Pure confirmed project membership; no filesystem or retrieval dependency."""
import ntpath
import hashlib
import unicodedata

from .models import CatalogFile, DocumentContext, ProjectAlias, ProjectDefinition

INPUT_SCHEMA = 'filename-path-root-project-v2'


def path_segments(value: str) -> tuple[str, ...]:
    """A canonical relative slash path, compared with Windows path casing."""
    if (type(value) is not str or '\0' in value or '\\' in value
            or ntpath.splitdrive(value)[0] or value.startswith('/')):
        raise ValueError('invalid_relative_path')
    if value == '':
        return ()
    parts = value.split('/')
    if any(not part or part in ('.', '..') for part in parts):
        raise ValueError('invalid_relative_path')
    return tuple(part.lower() for part in parts)


def project_for_path(source_id: int, relative_path: str,
                     projects: tuple[ProjectDefinition, ...]) -> ProjectDefinition | None:
    parts = path_segments(relative_path)
    matched = None
    for project in projects:
        if source_id not in project.source_ids:
            continue
        for binding in project.bindings:
            if not binding.confirmed or binding.source_id != source_id:
                continue
            prefix = path_segments(binding.path_prefix)
            if parts[:len(prefix)] == prefix:
                if matched is not None and matched.project_id != project.project_id:
                    raise ValueError('project_binding_conflict')
                matched = project
    return matched


def build_document_context(file: CatalogFile, *, source_label: str,
                           projects: tuple[ProjectDefinition, ...],
                           aliases: tuple[ProjectAlias, ...],
                           context_revision: int) -> DocumentContext:
    if type(context_revision) is not int or not 1 <= context_revision <= 2**63 - 1:
        raise ValueError('invalid_context_revision')
    project = project_for_path(file.source_id, file.relative_path, projects)
    variants = {unicodedata.normalize('NFKC', variant).casefold()
                for alias in aliases if project is not None and alias.confirmed
                and alias.project_id == project.project_id and file.source_id in alias.source_ids
                for variant in alias.variants}
    return DocumentContext(file.file_id, file.source_id, file.name, file.relative_path, file.root,
        source_label, project.project_id if project else None,
        project.canonical_name if project else None, tuple(sorted(variants)), context_revision)


def metadata_embedding_text(context: DocumentContext) -> str:
    return (f'name: {context.name}\nproject: {context.project_name or ""}\n'
            f'source: {context.source_label}\npath: {context.relative_path}\n'
            f'root: {context.root}\naliases: {", ".join(context.aliases)}')


def metadata_input_hash(context: DocumentContext) -> str:
    return hashlib.sha256(metadata_embedding_text(context).encode('utf-8')).hexdigest()
