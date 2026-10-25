/** Build-only consumer of the common PRODUCT export; never a second catalog. */
module.exports = {
  readProductMetadata() { throw new Error('product_metadata_invalid'); },
  stampPackageManifest() { throw new Error('product_metadata_invalid'); },
};
