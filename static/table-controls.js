/**
 * table-controls.js — Independent pagination, search, and filter for each
 * vote table on the BOS member profile page.
 *
 * Client-side mode:  default — searches/filters/paginates loaded DOM rows.
 * API-backed mode:   when .table-controls-wrapper has data-api-url —
 *                    searches, filters, and paginates via async fetch to the
 *                    server, so only the visible page is in the DOM.
 *
 * No framework dependency.
 */
(function () {
  'use strict';

  // ── Controller ──────────────────────────────────────────────────────────
  class TableController {
    constructor(el) {
      this.wrapper = el;
      this.tableId = el.getAttribute('data-table-id');
      this.apiUrl = el.getAttribute('data-api-url') || null;

      this.table = el.querySelector('table');
      this.tbody = this.table ? this.table.querySelector('tbody') : null;
      if (!this.tbody) return;

      // API-backed tables have the total count embedded in the info text
      this.apiTotal = 0;
      if (this.apiUrl) {
        // The initial page rows are pre-rendered in the HTML
        this.allRows = Array.from(this.tbody.querySelectorAll('tr'));
        this.apiTotal = this.allRows.length; // placeholder, will be overridden
      } else {
        // Client-side: store all rows for local filtering
        this.allRows = Array.from(this.tbody.querySelectorAll('tr'));
      }

      this.filteredRows = this.allRows.slice();
      this.page = 1;
      this.pageSize = 10;

      // Search inputs and filters
      this.searchInput = el.querySelector('.ts-search');
      this.pageSizeSelect = el.querySelector('.ts-page-size');
      this.filterSelects = el.querySelectorAll('select.ts-filter');
      this.infoEl = el.querySelector('.ts-info');
      this.prevBtn = el.querySelector('.ts-prev');
      this.nextBtn = el.querySelector('.ts-next');

      // Column header index mapping
      this.colMap = this._buildColumnMap();

      this._bind();
      this._render();
    }

    // ── Detect column positions from <thead> ────────────────────────────────
    _buildColumnMap() {
      const thead = this.table.querySelector('thead');
      if (!thead) return {};
      const ths = thead.querySelectorAll('th');
      const map = {};
      const text = Array.from(ths).map((th) =>
        th.textContent.trim().toLowerCase()
      );
      text.forEach((t, i) => {
        if (/^date|meeting date$/.test(t)) map.date = i;
        if (/^type$/.test(t) && !/meeting/.test(this.tableId)) map.type = i;
        if (/^meeting type|type$/.test(t)) map.meetingType = i;
        if (/^#|item$/.test(t)) map.itemNum = i;
        if (/^title$/.test(t)) map.title = i;
        if (/^vote$/.test(t)) map.vote = i;
        if (/^result$/.test(t)) map.result = i;
        if (/^majority$/.test(t) && !/alignment/.test(t)) map.majorityPos = i;
        if (/^alignment$/.test(t)) map.alignment = i;
        if (/^c.number|c-number/.test(t)) map.cNumber = i;
        if (/^split/.test(t)) map.split = i;
        if (/^controversy|reason/.test(t)) map.controversy = i;
        if (/^tally$/.test(t)) map.tally = i;
        if (/^prevailing/.test(t)) map.prevailing = i;
      });
      return map;
    }

    // ── Event binding ──────────────────────────────────────────────────────
    _bind() {
      let debounceTimer;

      if (this.searchInput) {
        this.searchInput.addEventListener('input', () => {
          clearTimeout(debounceTimer);
          debounceTimer = setTimeout(() => {
            this.page = 1;
            this._render();
          }, 300);
        });
      }

      if (this.pageSizeSelect) {
        this.pageSizeSelect.addEventListener('change', () => {
          this.pageSize = parseInt(this.pageSizeSelect.value, 10);
          this.page = 1;
          this._render();
        });
      }

      this.filterSelects.forEach((sel) => {
        sel.addEventListener('change', () => {
          this.page = 1;
          this._render();
        });
      });

      if (this.prevBtn) {
        this.prevBtn.addEventListener('click', (e) => {
          e.preventDefault();
          if (this.page > 1) {
            this.page--;
            this._render();
          }
        });
      }

      if (this.nextBtn) {
        this.nextBtn.addEventListener('click', (e) => {
          e.preventDefault();
          const maxPage = this._maxPage();
          if (this.page < maxPage) {
            this.page++;
            this._render();
          }
        });
      }
    }

    // ── Client-side filtering (non-API tables) ─────────────────────────────
    _matchesFilters(row) {
      const tds = row.querySelectorAll('td');

      if (this.searchInput) {
        const q = this.searchInput.value.trim().toLowerCase();
        if (q) {
          let found = false;
          for (let i = 0; i < tds.length; i++) {
            if (tds[i].textContent.toLowerCase().includes(q)) {
              found = true;
              break;
            }
          }
          if (!found) return false;
        }
      }

      for (const sel of this.filterSelects) {
        const val = sel.value;
        if (!val) continue;
        const colKey = sel.getAttribute('data-filter-col');
        if (!colKey) continue;
        const colIdx = this.colMap[colKey];
        if (colIdx === undefined || colIdx >= tds.length) continue;
        const cellText = tds[colIdx].textContent.trim().toLowerCase();
        if (cellText !== val) return false;
      }

      return true;
    }

    // ── Max page (client-side) ─────────────────────────────────────────────
    _maxPage() {
      if (this.apiUrl) {
        return Math.max(1, Math.ceil(this.apiTotal / this.pageSize));
      }
      return Math.max(1, Math.ceil(this.filteredRows.length / this.pageSize));
    }

    // ── Render dispatch ────────────────────────────────────────────────────
    _render() {
      if (this.apiUrl) {
        this._fetchAndRender();
      } else {
        this._renderClientSide();
      }
    }

    // ── Client-side render: filter + paginate in-DOM rows ──────────────────
    _renderClientSide() {
      this.filteredRows = this.allRows.filter((r) => this._matchesFilters(r));
      const total = this.filteredRows.length;
      const maxPage = this._maxPage();
      if (this.page > maxPage) this.page = maxPage;
      const start = (this.page - 1) * this.pageSize;
      const end = Math.min(start + this.pageSize, total);
      const pageRows = this.filteredRows.slice(start, end);

      this._renderRows(pageRows, total, start, end, maxPage);
    }

    // ── API-backed render: fetch filtered data from server ─────────────────
    _fetchAndRender() {
      const params = new URLSearchParams();
      params.set('page', this.page);
      params.set('per_page', this.pageSize);

      if (this.searchInput) {
        const q = this.searchInput.value.trim();
        if (q) params.set('q', q);
      }

      this.filterSelects.forEach((sel) => {
        const val = sel.value;
        if (val) {
          const colKey = sel.getAttribute('data-filter-col');
          if (colKey) params.set(colKey, val);
        }
      });

      const url = this.apiUrl + '?' + params.toString();

      // Show loading indicator
      if (this.tbody) {
        this.tbody.innerHTML =
          '<tr><td colspan="10" class="text-center text-muted py-3">Loading...</td></tr>';
      }

      fetch(url)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          this.apiTotal = data.total;
          const rows = data.rows;
          const page = data.page;
          const perPage = data.per_page;
          const start = (page - 1) * perPage;
          const end = Math.min(start + perPage, this.apiTotal);
          const maxPage = this._maxPage();

          // Build HTML rows from JSON data
          const pageRows = rows.map(function (r) { return this._buildRow(r); }.bind(this));

          this._renderRows(pageRows, this.apiTotal, start, end, maxPage);
        }.bind(this))
        .catch(function () {
          if (this.tbody) {
            this.tbody.innerHTML =
              '<tr><td colspan="10" class="text-center text-danger py-3">Error loading data.</td></tr>';
          }
        }.bind(this));
    }

    // ── Build a single <tr> from a JSON vote record ────────────────────────
    _buildRow(r) {
      const tr = document.createElement('tr');

      // Map: meeting_id, meeting_date, meeting_type, agenda_item_number,
      //      agenda_item_title, c_number, vote, motion_result,
      //      is_split_vote, majority_position, with_or_against_majority
      const cells = [
        { text: r.meeting_date, link: '/meetings/' + (r.meeting_id || '') },
        { text: r.meeting_type || '' },
        { text: String(r.agenda_item_number || '') },
        { text: r.agenda_item_title ? r.agenda_item_title.slice(0, 50) + (r.agenda_item_title.length > 50 ? '...' : '') : '\u2014' },
        { text: r.c_number || '\u2014', code: !!r.c_number },
        { text: r.vote || '', badge: this._badgeClass(r.vote || '', r.is_inferred), title: r.is_inferred ? 'Inferred — vote not found in meeting summary. May indicate a parser gap.' : '' },
        { text: r.motion_result || '', badge: 'secondary' },
        { text: r.is_split_vote ? 'Split' : '\u2014', badge: r.is_split_vote ? 'warning text-dark' : '' },
        { text: r.majority_position || '\u2014', badge: r.majority_position ? this._badgeClass(r.majority_position) : '' },
        { text: r.with_or_against_majority ? this._titleCase(r.with_or_against_majority.replace(/_/g, ' ')) : '\u2014', badge: r.with_or_against_majority === 'with_majority' ? 'success' : r.with_or_against_majority === 'against_majority' ? 'danger' : '' },
      ];

      cells.forEach(function (c) {
        const td = document.createElement('td');
        if (c.link) {
          const a = document.createElement('a');
          a.href = c.link;
          a.textContent = c.text;
          td.appendChild(a);
        } else if (c.code) {
          const code = document.createElement('code');
          code.textContent = c.text;
          td.appendChild(code);
        } else if (c.badge) {
          const span = document.createElement('span');
          span.className = 'badge bg-' + c.badge;
          span.textContent = c.text;
          if (c.title) span.title = c.title;
          td.appendChild(span);
        } else {
          td.textContent = c.text;
        }
        tr.appendChild(td);
      });

      return tr;
    }

    _badgeClass(val, isInferred) {
      if (isInferred) return 'info text-dark';
      var v = (val || '').toLowerCase();
      if (v === 'yes' || v === 'aye') return 'success';
      if (v === 'no' || v === 'nay') return 'danger';
      if (v === 'abstain') return 'warning';
      if (v === 'absent') return 'secondary';
      return 'secondary';
    }

    _titleCase(str) {
      return str.replace(/\w\S*/g, function (t) {
        return t.charAt(0).toUpperCase() + t.slice(1).toLowerCase();
      });
    }

    // ── Render rows into tbody ─────────────────────────────────────────
    _renderRows(pageRows, total, start, end, maxPage) {
      if (!this.tbody) return;

      this.tbody.innerHTML = '';

      if (pageRows.length === 0) {
        var colspan = this.table.querySelector('thead th') ?
          this.table.querySelectorAll('thead th').length : 1;
        var emptyRow = document.createElement('tr');
        var emptyTd = document.createElement('td');
        emptyTd.setAttribute('colspan', colspan);
        emptyTd.className = 'text-muted text-center py-3';
        emptyTd.textContent = 'No matching records found.';
        emptyRow.appendChild(emptyTd);
        this.tbody.appendChild(emptyRow);
      } else {
        for (var i = 0; i < pageRows.length; i++) {
          this.tbody.appendChild(pageRows[i]);
        }
      }

      if (this.infoEl) {
        if (total === 0) {
          this.infoEl.textContent = '0 records';
        } else {
          this.infoEl.textContent = 'Showing ' + (start + 1) + '\u2013' + end + ' of ' + total + ' records';
        }
      }

      if (this.prevBtn) {
        this.prevBtn.classList.toggle('disabled', this.page <= 1);
      }
      if (this.nextBtn) {
        this.nextBtn.classList.toggle('disabled', this.page >= maxPage);
      }
    }
  }

  // ── Init ─────────────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    document
      .querySelectorAll('.table-controls-wrapper[data-table-id]')
      .forEach(function (el) {
        new TableController(el);
      });
  });
})();
