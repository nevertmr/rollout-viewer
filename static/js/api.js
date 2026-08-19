"use strict";
/* 서버 API 호출 */
async function getJSON(url, signal, init){
  const r = await fetch(url, Object.assign({signal}, init || {}));
  if(!r.ok) throw new Error(url+" → "+r.status);
  return r.json();
}
