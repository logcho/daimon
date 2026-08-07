import fs from "node:fs/promises";
import path from "node:path";
import ExcelJS from "exceljs";

// Same shape as `vault.ts`'s `VAULT_DIR` — a plain host directory passed in
// as an env var (see `src-tauri/src/workspace.rs`'s `documents_dir`), not a
// bind mount (native processes have no mount namespace to rely on).
export const DOCUMENTS_DIR = process.env.DAIMON_DOCUMENTS_DIR ?? "/workspace/documents";

/** Same reasoning as vault.ts's `sanitizeFilename` — never trust a string argument for a filesystem path. */
function sanitizeFilename(filename: string): string {
  const base = path.basename(filename).trim();
  if (!base || base === "." || base === "..") {
    throw new Error(`invalid document filename: ${JSON.stringify(filename)}`);
  }
  return base.toLowerCase().endsWith(".xlsx") ? base : `${base}.xlsx`;
}

/**
 * Writes a real `.xlsx` file — chosen over trying to drive a live Excel
 * window (via AppleScript or otherwise) because Excel's own scripting
 * support on macOS is inconsistent/partial, a poor automation target
 * compared to just producing the actual file directly. `rows` is a plain
 * grid (first row conventionally a header) rather than a cell-by-cell API,
 * matching what a model can reliably produce without needing to reason
 * about spreadsheet-library specifics.
 */
export async function writeSpreadsheet(filename: string, sheetName: string, rows: string[][]): Promise<string> {
  const safeName = sanitizeFilename(filename);
  await fs.mkdir(DOCUMENTS_DIR, { recursive: true });

  const workbook = new ExcelJS.Workbook();
  const sheet = workbook.addWorksheet(sheetName || "Sheet1");
  for (const row of rows) {
    sheet.addRow(row);
  }
  // Bold header row + auto-sized-ish columns — cheap, makes the output look
  // like a real spreadsheet someone made on purpose rather than a raw data
  // dump, with no per-cell styling calls the model would need to request.
  if (rows.length > 0) {
    sheet.getRow(1).font = { bold: true };
  }
  sheet.columns.forEach((column) => {
    let maxLength = 10;
    column.eachCell?.({ includeEmpty: false }, (cell) => {
      maxLength = Math.max(maxLength, String(cell.value ?? "").length);
    });
    column.width = Math.min(maxLength + 2, 60);
  });

  const destPath = path.join(DOCUMENTS_DIR, safeName);
  await workbook.xlsx.writeFile(destPath);
  return destPath;
}
