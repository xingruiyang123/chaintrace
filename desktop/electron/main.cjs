const {app,BrowserWindow,ipcMain,dialog} = require('electron');
const {spawn} = require('node:child_process');
const path = require('node:path');
const fs=require('node:fs/promises');
let activeInput=null,selectedInput=null,hasResult=false,lastResult=null,lastInput=null;
const root=path.resolve(__dirname,'..');
function python(action,extra={}) {return new Promise((resolve,reject)=>{
 const exe=process.env.CHAINTRACE_PYTHON || 'python';
 const proc=spawn(exe,['-X','utf8',path.join(root,'离线原型','desktop_bridge.py')],{cwd:root,windowsHide:true});
 let out='',err='';proc.stdout.setEncoding('utf8');proc.stderr.setEncoding('utf8');proc.stdout.on('data',x=>out+=x);proc.stderr.on('data',x=>err+=x);
 proc.on('error',reject);proc.on('close',code=>{try{const data=JSON.parse(out);if(code!==0||!data.ok)reject(new Error(data.error||err||'核验服务失败'));else resolve(data)}catch(e){reject(e)}});
 proc.stdin.end(JSON.stringify({action,...extra}));
})}
ipcMain.handle('choose-input',async()=>{const r=await dialog.showOpenDialog({properties:['openFile'],filters:[{name:'JSON',extensions:['json']}]});if(r.canceled)return null;selectedInput=r.filePaths[0];return selectedInput});
ipcMain.handle('sample',()=>{selectedInput=path.join(root,'离线原型','样例','综合模拟样例.json');return selectedInput});
ipcMain.handle('run',async(_e,file)=>{if(typeof file!=='string'||!file||file!==selectedInput)throw Error('请选择模拟输入');hasResult=false; lastResult=null; lastInput=null; const res=await python('run',{path:file});activeInput=file;lastResult=res.result;lastInput=res.input;hasResult=true;return {result:res.result,input:res.input,timelineTimes:res.timeline_times}});
ipcMain.handle('export',async()=>{if(!hasResult||!activeInput)throw Error('请先完成核验');const r=await dialog.showOpenDialog({properties:['openDirectory','createDirectory']});if(r.canceled)return null;if((await fs.readdir(r.filePaths[0])).length)throw Error('请选择空文件夹，避免覆盖已有文件。');return (await python('export',{result:lastResult,input:lastInput,output_path:r.filePaths[0]})).log});
app.whenReady().then(()=>{const w=new BrowserWindow({width:1400,height:900,minWidth:1000,minHeight:700,show:true,autoHideMenuBar:true,titleBarStyle:'hidden',titleBarOverlay:{color:'#f7f8fa',symbolColor:'#303747',height:40},icon:path.join(__dirname,'logo.ico'),backgroundColor:'#e9edf5',title:'链迹核验 · 模拟数据离线原型',webPreferences:{preload:path.join(__dirname,'preload.cjs'),contextIsolation:true,nodeIntegration:false,sandbox:true}});w.setMenu(null);if(process.argv.includes('--dev'))w.loadURL('http://127.0.0.1:5173');else w.loadFile(path.join(root,'dist','index.html'));});
app.on('window-all-closed',()=>app.quit());
