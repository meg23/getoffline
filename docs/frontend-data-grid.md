# Library data grid

The Library uses [Tabulator](https://tabulator.info/) to provide a richer data
grid without introducing React or a frontend build pipeline. Django continues
to own authentication and the page shell, and the API remains the source of
truth for library and job data.

## Why Tabulator

The Library contains both durable downloads and temporary processing rows.
Those rows change from queued through downloading, transcoding, transcription,
and ready states while the page is open. Tabulator's object-based row model is
a better fit for those live updates than continuing to rebuild a server-rendered
HTML table by hand.

Tabulator also provides the table quality-of-life features expected here:

- sortable, resizable, and drag-reorderable columns;
- a column menu for showing and hiding fields;
- persisted column widths, order, visibility, and sorting;
- responsive column collapse and virtualized rendering; and
- custom formatters for media links, status pills, and batch selection.

The existing Django table remains in `library.html` as the initial render and
no-JavaScript fallback. When JavaScript is available, `dashboard.js` reads that
initial data, initializes Tabulator, hides the fallback table, and refreshes the
grid from `/api/frontend/library`. Active job polling merges processing state
into the same rows before replacing the grid data.

## Files and dependency policy

- `src/frontend/templates/app/library.html` loads the grid and retains the
  fallback table.
- `src/frontend/static/app/dashboard.js` contains the column definitions,
  formatters, filtering, selection, persistence, and refresh integration.
- `src/frontend/static/app/dashboard.css` adapts Tabulator to the GetOffline
  visual design.
- `src/frontend/static/vendor/tabulator/` contains the pinned Tabulator 6.3.1
  distribution and its MIT license.

The dependency is vendored intentionally. GetOffline is self-hosted, so the UI
must not require a public CDN at runtime. When upgrading Tabulator, replace the
JavaScript, CSS, and license together, update the version in this document, and
test column persistence because persisted configuration can outlive a release.

## Adding or changing columns

Edit the `columns` array in `initializeLibraryGrid` in `dashboard.js`. Prefer a
plain `field` for text data. Use a formatter only for interactive or styled
content, and create DOM nodes rather than interpolating untrusted metadata into
HTML. Every persisted column needs a stable field name.

After changing columns, verify at minimum:

1. initial server-rendered rows and API-refreshed rows contain the same data;
2. queued and running jobs appear and update without losing selections;
3. column resizing, reordering, visibility, and sorting survive a reload;
4. batch actions submit only the selected download IDs; and
5. playback links, mobile layout, and keyboard focus still work.
