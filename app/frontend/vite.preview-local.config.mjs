import { mergeConfig } from 'vite';
import config from './vite.config.ts';
export default mergeConfig(config, {server:{port:5199,proxy:Object.fromEntries(['/api','/health','/mcp'].map(path=>[path,{target:'http://127.0.0.1:8431',changeOrigin:false}]))}});
