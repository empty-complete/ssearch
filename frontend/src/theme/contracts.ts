export type PaletteMode = 'light' | 'dark';
export type SemanticColorToken =
  | 'background' | 'surface' | 'surface-secondary' | 'foreground' | 'muted'
  | 'accent' | 'accent-foreground' | 'border' | 'focus' | 'danger' | 'warning'
  | 'selected' | 'field-background' | 'field-foreground' | 'field-placeholder' | 'field-border';

export type SemanticColors<Color> = { readonly [Token in SemanticColorToken]: Color };
type HexDigit = '0' | '1' | '2' | '3' | '4' | '5' | '6' | '7' | '8' | '9' | 'a' | 'b' | 'c' | 'd' | 'e' | 'f';
export type ColorByte = `${HexDigit}${HexDigit}`;
export type RgbBytes = readonly [red: ColorByte, green: ColorByte, blue: ColorByte];

/** Data shape only. P5/P2 ingestion, validation and atomic settings ownership remain separate. */
export interface CustomPaletteInputV1 {
  readonly schemaVersion: 1;
  readonly mode: PaletteMode;
  readonly tokens: SemanticColors<RgbBytes>;
}

export interface PaletteSource {
  readonly repository: string;
  readonly revision: string;
  readonly paletteSnapshot: string;
  readonly licenseSnapshot: string;
}

export type SemanticPaletteInput = {
  readonly id: 'catppuccin';
  readonly status: 'default';
  readonly availableModes: readonly ['light', 'dark'];
  readonly lightComposition: 'default_pair';
  readonly source: PaletteSource;
  readonly tokens: Readonly<Record<PaletteMode, SemanticColors<string>>>;
} | {
  readonly id: 'nord' | 'dracula';
  readonly status: 'prepared';
  readonly availableModes: readonly ['dark'];
  readonly lightComposition: 'requires_explicit_choice';
  readonly source: PaletteSource;
  readonly tokens: { readonly dark: SemanticColors<string> };
};
