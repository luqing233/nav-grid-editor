let database;
function openDatabase() {
  if (database) return database;
  database = new Promise((resolve, reject) => {
  const request = indexedDB.open("nav-grid-editor-drafts", 1);
  request.onupgradeneeded = () => request.result.createObjectStore("drafts", {keyPath:"id"});
  request.onsuccess = () => resolve(request.result);
  request.onerror = () => reject(request.error);
  });
  return database;
}
async function transact(mode, action) {
  const db = await openDatabase();
  return new Promise((resolve,reject)=>{
    const transaction=db.transaction("drafts",mode);
    const request=action(transaction.objectStore("drafts"));
    transaction.oncomplete=()=>resolve(request.result);
    transaction.onerror=()=>reject(transaction.error);
    transaction.onabort=()=>reject(transaction.error || new Error("草稿事务已取消"));
  });
}
export const storeDraft = draft => transact("readwrite", store=>store.put(draft));
export const removeDraft = id => transact("readwrite", store=>store.delete(id));
export const listDrafts = () => transact("readonly", store=>store.getAll());
export const clearDrafts = () => transact("readwrite", store=>store.clear());
