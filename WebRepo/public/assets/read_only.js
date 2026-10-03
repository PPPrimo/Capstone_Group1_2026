(function(){
  function preventContextMenu(){
    document.addEventListener('contextmenu', function(e){ e.preventDefault(); }, {capture:true});
  }
  function preventKeyDownloads(){
    window.addEventListener('keydown', function(e){
      const key = e.key.toLowerCase();
      const ctrl = e.ctrlKey || e.metaKey;
      // Block common save/print/devtools combos
      if (ctrl && (key === 's' || key === 'p' || key === 'u')) {
        e.preventDefault();
        e.stopPropagation();
      }
      // Block F12
      if (e.key === 'F12') {
        e.preventDefault();
        e.stopPropagation();
      }
    }, {capture:true});
  }
  function preventDragSave(){
    document.addEventListener('dragstart', function(e){ e.preventDefault(); }, {capture:true});
  }
  function sanitizeAnchors(){
    const anchors = document.querySelectorAll('a');
    anchors.forEach(a => {
      if (a.hasAttribute('download')) a.removeAttribute('download');
      a.setAttribute('rel', 'noopener noreferrer');
      // Force same-tab to reduce file save prompts
      a.setAttribute('target', '_self');
    });
  }
  function disableSelection(){
    const style = document.createElement('style');
    style.innerHTML = 'html, body { -webkit-user-select: none; -ms-user-select: none; user-select: none; }';
    document.head.appendChild(style);
  }
  function init(){
    preventContextMenu();
    preventKeyDownloads();
    preventDragSave();
    sanitizeAnchors();
    disableSelection();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
