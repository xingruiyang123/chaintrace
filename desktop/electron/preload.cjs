const {contextBridge,ipcRenderer}=require('electron');
contextBridge.exposeInMainWorld('chaintrace',{chooseInput:()=>ipcRenderer.invoke('choose-input'),sample:()=>ipcRenderer.invoke('sample'),run:(file)=>ipcRenderer.invoke('run',file),export:()=>ipcRenderer.invoke('export')});
