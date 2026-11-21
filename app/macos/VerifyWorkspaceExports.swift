import Cocoa
import WebKit

// --verify-ui uses only the dedicated synthetic fixture built by verify_native.py.
extension Desktop {
    static let fixtureExportNames = ["Senrelay-profile-v1.txt", "SKILL.md", "Senrelay-collaboration.txt"]

    func verifyNextExport() {
        guard exportStage < Self.fixtureExportNames.count else { NSApp.terminate(nil); return }
        let script = """
        const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
        const wait = async when => {
          for (let n=0; n<100; n++) { const item = when(); if (item) return item; await pause(50); }
          throw new Error('Expected export control was unavailable');
        };
        const stages = [
          ['personal','profile','profile'], ['personal','assets','skill'], ['exchange','','exchange']
        ];
        const [page, section, kind] = stages[stage];
        const nav = '[data-route-page="' + page + '"]' + (section ? '[data-route-section="' + section + '"]' : '');
        (await wait(()=>document.querySelector(nav))).click();
        const button = await wait(()=>document.querySelector('[data-export-kind="' + kind + '"]'));
        if (kind === 'skill') button.closest('details')?.setAttribute('open','');
        if (kind === 'exchange') {
          const select = document.querySelector('.exchange-fields select');
          select.value = '0'; select.dispatchEvent(new Event('change',{bubbles:true}));
          const input = document.querySelector('.exchange-fields textarea');
          Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input,'Native delivery fixture');
          input.dispatchEvent(new Event('input',{bubbles:true}));
        }
        await wait(()=>!button.disabled && button.getBoundingClientRect().height > 0);
        button.click();
        return true;
        """
        web.callAsyncJavaScript(script, arguments: ["stage": exportStage], in: nil, in: .page) { outcome in
            if case .failure(let error) = outcome { self.fail("Native export action failed: " + error.localizedDescription) }
        }
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard verificationDirectory != nil, verifyScenario == "workspace-exports" else { return }
        exportStage += 1
        verifyNextExport()
    }
}
