# Browser dependencies

The Designer and Control Panel load these pinned, local distributions through
`/static/vendor/`. They are copied unchanged from the npm package tarballs so
the graph editor, layout, and 3D scene load without a CDN connection.

| Package | Version | npm tarball SHA-1 | Included files |
| --- | --- | --- | --- |
| `litegraph.js` | 0.7.18 | `6c7db4c57ca7428f295c778c38f0605c3f4a13fd` | Minified build, CSS, license |
| `dagre` | 0.8.5 | `ba30b0055dac12b6c1fcc247817442777d06afee` | Minified distribution, license |
| `three` | 0.162.0 | `b15a511f1498e0c42d4d00bbb411c7527b06097e` | ES module build, the five addon modules used by pyBravo and their dependency, license |

`SHA256SUMS` records the copied files. The package license for each library is
beside its distribution. To update one, review its browser API compatibility,
replace the pinned files from the new package, update both HTML import paths,
and regenerate the checksums.
