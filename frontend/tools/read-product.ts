import { readFileSync } from 'node:fs';
import { join } from 'node:path';

/** Temporary build-time adapter to the existing single metadata owner.
 * The owner namespace migration remains open; do not introduce a second catalog.
 */
export function readProductMetadata(repositoryRoot: string): Record<string, string> {
  const catalog = readFileSync(join(repositoryRoot, 'src/atom_search/content.py'), 'utf8');
  const block = catalog.match(/PRODUCT = (\{[\s\S]*?\n\})/)?.[1];
  if (!block) throw new Error('PRODUCT catalog is missing');
  const product = JSON.parse(block) as Record<string, string>;
  for (const key of ['product_name', 'product_name_ru', 'support_email', 'author_name']) {
    if (!product[key]) throw new Error(`PRODUCT.${key} is missing`);
  }
  return product;
}
