/** Real browser acceptance against an isolated simulation server.
 * PYBRAVO_BROWSER_TEST_URL=http://127.0.0.1:8771 node tests/browser/protocol_designer_roundtrip.cjs
 * Imports a synthetic PDF, reviews/edits a Wait/Manual plan in Protocol Assistant,
 * validates/simulates/approves it, and checks Designer Save preserves execution
 * semantics and provenance. Never calls hardware execution or a language model.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('playwright');
const base = process.env.PYBRAVO_BROWSER_TEST_URL;
if (!base) throw new Error('Set PYBRAVO_BROWSER_TEST_URL to an isolated simulation server.');
async function api(path) {
    const response = await fetch(base + path);
    const data = await response.json();
    if (!response.ok) throw new Error(JSON.stringify(data));
    return data;
}
function execution(workflow) {
    return {
        deck: workflow.deck || {},
        nodes: (workflow.graph?.nodes || []).map(node => ({
            id: node.id, type: node.type, properties: node.properties || {},
            inputs: (node.inputs || []).map(port => ({ link: port.link ?? null })),
            outputs: (node.outputs || []).map(port => ({ links: port.links || [] })),
        })).sort((a, b) => a.id - b.id),
        links: (workflow.graph?.links || []).sort((a, b) => a[0] - b[0]),
        library: workflow.library || '',
    };
}
function sourcePdf() {
    const stream = 'BT /F1 12 Tf 40 200 Td (Inspect the plate manually. Wait 1 s.) Tj ET';
    const objects = [
        '<< /Type /Catalog /Pages 2 0 R >>',
        '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 300] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`,
    ];
    let output = '%PDF-1.4\n';
    const offsets = [0];
    objects.forEach((object, index) => { offsets.push(output.length); output += `${index + 1} 0 obj\n${object}\nendobj\n`; });
    const xref = output.length;
    output += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
    offsets.slice(1).forEach(offset => { output += `${String(offset).padStart(10, '0')} 00000 n \n`; });
    output += `trailer << /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`;
    return Buffer.from(output);
}
(async () => {
    const context = await api('/api/protocols/context');
    assert.equal(context.controller_type, 'simulation', 'Use an isolated simulation server for this test.');
    const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome', headless: true });
    const page = await browser.newPage({ viewport: { width: 1500, height: 1050 }, ignoreHTTPSErrors: true });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const fixture = sourcePdf();
    try {
        await page.goto(base + '/protocol-assistant');
        await page.locator('#protocol-file').setInputFiles({ name: 'Browser acceptance.pdf', mimeType: 'application/pdf', buffer: fixture });
        await page.locator('#ingest').click();
        await page.locator('#original-pdf').waitFor({ state: 'visible' });
        const identity = new URL(page.url()).searchParams.get('session');
        const pdfHref = await page.locator('#original-pdf').getAttribute('href');
        assert.equal(pdfHref, `/api/protocols/${identity}/source-pdf`);
        const pdfResponse = await fetch(base + pdfHref);
        assert.equal(pdfResponse.status, 200);
        assert.equal(pdfResponse.headers.get('content-type'), 'application/pdf');
        assert.deepEqual(Buffer.from(await pdfResponse.arrayBuffer()), fixture, 'The PDF link must return the original uploaded bytes');
        assert.match(await page.locator('#source-paragraphs').innerText(), /Page 1/);
        assert.match(await page.locator('#source-paragraphs').innerText(), /Wait 1 s/);
        await page.locator('#source-reviewed').check();
        await page.locator('#tab-plan').click();
        await page.locator('#prepare-deck').click();
        await page.locator('#correction-reason').fill('Synthetic browser acceptance: confirmed from the uploaded PDF.');
        await page.locator('#add-step').click();
        await page.locator('[data-path="/steps/0/description"]').fill('Inspect the plate');
        await page.locator('[data-path="/steps/0/message"]').fill('Inspect the plate manually.');
        await page.locator('[data-path="/steps/0/message"]').blur();
        await page.locator('#add-step').click();
        await page.locator('[data-path="/steps/1/kind"]').selectOption('wait');
        await page.locator('[data-path="/steps/1/description"]').fill('Wait before continuing');
        await page.locator('[data-path="/steps/1/duration_s"]').fill('1');
        await page.locator('[data-path="/steps/1/duration_s"]').blur();
        await page.locator('#tab-review').click();
        assert.equal(await page.locator('#approve').isDisabled(), true);
        await page.locator('#validate').click();
        await page.waitForFunction(() => document.querySelector('#validation-status').textContent === 'Passed');
        await page.locator('#simulate').click();
        await page.waitForFunction(() => document.querySelector('#simulation-status').textContent === 'Passed');
        await page.locator('#scientist').fill('Automated isolated browser test');
        await page.locator('#approval-notes').fill('Synthetic qualification fixture; no hardware has been used.');
        await page.locator('#plan-reviewed').check();
        await page.locator('#deck-confirmed').check();
        await page.locator('#approve').click();
        await page.waitForFunction(() => document.querySelector('#approval-status').textContent === 'Approved');
        await page.screenshot({ path: '/tmp/protocol-assistant-final-acceptance.png', fullPage: true });
        await page.locator('#export').click();
        await page.waitForURL(url => url.pathname === '/designer' && url.searchParams.has('workflow'));
        const workflowId = new URL(page.url()).searchParams.get('workflow');
        const before = await api('/api/workflows/' + workflowId);
        await page.waitForFunction(() => document.querySelector('.wf-tab-name')?.textContent === 'Browser acceptance.pdf', {}, { timeout: 60000 });
        await page.screenshot({ path: '/tmp/protocol-designer-roundtrip.png' });
        await page.locator('#btn-save').click();
        await page.locator('#workflow-save-btn').click();
        await page.waitForFunction(() => document.querySelector('#workflow-modal').classList.contains('hidden'));
        const after = await api('/api/workflows/' + workflowId);
        fs.writeFileSync('/tmp/protocol-designer-before.json', JSON.stringify(before, null, 2));
        fs.writeFileSync('/tmp/protocol-designer-after.json', JSON.stringify(after, null, 2));
        assert.deepEqual(execution(after), execution(before), 'Opening and saving must preserve the exact executable graph');
        for (const key of ['protocol_session_id', 'protocol_revision', 'protocol']) assert.deepEqual(after[key], before[key], key + ' metadata must survive');
        assert.deepEqual(errors, [], 'Assistant and Designer must not throw page errors');
        console.log(`Real PDF import/link → scientist edits → strict simulation → approval → Assistant export → Designer open/save passed. Session ${identity}; workflow ${workflowId}`);
    } finally {
        await browser.close();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
