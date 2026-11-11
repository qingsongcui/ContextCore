/** Copy the imagegen-extracted original PNG unchanged. Never redraw or modify its pixels. */
import { copyFile, readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)));
const logo=path.join(root,'brand/assets/senrelay-logo-original-v1.png');
const sha=buffer=>createHash('sha256').update(buffer).digest('hex');
const sourceHash=sha(await readFile(logo));
for(const target of ['website/assets/senrelay-logo-original-v1.png','app/frontend/public/senrelay-logo-original-v1.png','app/frontend/public/favicon.png','app/frontend/public/app-icon.png']){
 const destination=path.join(root,target);await copyFile(logo,destination);
 if(sha(await readFile(destination))!==sourceHash)throw new Error(`PNG bytes changed: ${target}`);
}
console.log(`Copied original imagegen PNG bytes unchanged. SHA-256: ${sourceHash}`);
