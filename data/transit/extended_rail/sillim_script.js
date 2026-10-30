// 노선 조회
//var arr_station = ["", "소사", "소새울", "시흥대야", "신천", "신현", "시흥시청", "시흥능곡", "달미", "선부", "초지", "시우", "원시"];
/*
var arr_station = [
    {"code": "", "value": ""},
    {"code": "4804", "value": "샛강"},
    {"code": "4805", "value": "대방"},
    {"code": "4806", "value": "서울지방병무청"},
    {"code": "4807", "value": "보라매"},
    {"code": "4808", "value": "보라매공원"},
    {"code": "4809", "value": "보라매병원"},
    {"code": "4810", "value": "당곡"},
    {"code": "4811", "value": "신림"},
    {"code": "4812", "value": "서원"},
    {"code": "4813", "value": "서울대벤쳐타운"},
    {"code": "4814", "value": "관악산(서울대)"}
];
*/

var arr_station;

$.getJSON( "/_prog/sttn_info_mng/ajax_hp_stn_info_sttn_data.php", function( data ) {

	arr_station = data;
});


var station = ["", ""];

$(document).ready(function () {
	

	
    $('.railroad-map > .line > li > button').on("click", function () {
            $(this).parent().addClass('on').siblings().removeClass("on");
        }
    );

    $('.button-bar button.btn-start').click( // 출발역
        function () {
            $(this).parent().parent().parent().addClass('start').siblings().removeClass("start");
            $(this).parent().parent().parent().removeClass("on");

            var sid = $(this).parent().parent().parent().prop("id");
            //station[0] = arr_station[parseInt(sid.split("-")[1])].code;
            station[0] = parseInt(sid.split("-")[1]);

            $("#start-station").val(station[0]);
            // jquery ui select
            // $('#start-station').selectmenu("refresh");
            search(station, "auto");
            
        }
    );

    $('.button-bar button.btn-arrival').click( // 도착역
        function () {
            $(this).parent().parent().parent().addClass('arrival').siblings().removeClass("arrival");
            $(this).parent().parent().parent().removeClass("on");

            var aid = $(this).parent().parent().parent().prop("id");
            //station[1] = arr_station[parseInt(aid.split("-")[1])].code;
            station[1] = parseInt(aid.split("-")[1]);

            $("#arrival-station").val(station[1]);
            // jquery ui select
            // $('#arrival-station').selectmenu("refresh");
            search(station, "auto");
            
        }
    );

    /*
    $(".stationSelector").selectmenu({
        //셀렉트에서 값 & 이미지 바인딩
        change: function (event, ui) {
            // console.log("change: function( event, ui )");
            var tgid = $(event.target).prop("id");
            var stationIdx = "0";

            $.each(arr_station, function (idx, o) {
                if (o.code == ui.item.value) {
                    stationIdx = (idx > 9) ? idx : "0" + idx;
                }
            });

            if (tgid == "start-station") {
                station[0] = ui.item.value;
                $('li#station-' + stationIdx + ' .button-bar button.btn-start').trigger("click");
            } else {
                station[1] = ui.item.value;
                $('li#station-' + stationIdx + ' .button-bar button.btn-arrival').trigger("click");
            }
        }
    });
    */
    
    $('.start_select').click(function(){    // 출발역 클릭시 arrow 이미지 변환
        if($(this).hasClass("select_show") === true){
            $(this).removeClass("select_show");
        }
        else{
            $(this).addClass("select_show");
        }
        $(this).blur(function(){
            $(this).removeClass("select_show");
        });
    });
    $('.arrival_select').click(function(){  // 도착역 클릭시 arrow 이미지 변환
        if($(this).hasClass("select_show") === true){
            $(this).removeClass("select_show");
        }
        else{
            $(this).addClass("select_show");
        }
        $(this).blur(function(){
            $(this).removeClass("select_show");
        });
    });

    $(".stationSelector").on("change", function(event){
        //셀렉트에서 값 & 이미지 바인딩
        var tgid = $(event.target).prop("id");
        var vv = event.target.value;
        var stationIdx = "0";
        
        $.each(arr_station, function (idx, o) {
        	
            if (o.code == vv) {
                stationIdx = (idx > 9) ? idx : "0" + idx;
            }
        });
        
        

        
        if (tgid == "start-station") {
            station[0] = vv;
            //$('li#station-' + stationIdx + ' .button-bar button.btn-start').trigger("click");
            $('li#station-' + vv + ' .button-bar button.btn-start').trigger("click");
        } else {
            station[1] = vv;
            //$('li#station-' + stationIdx + ' .button-bar button.btn-arrival').trigger("click");
            $('li#station-' + vv + ' .button-bar button.btn-arrival').trigger("click");
        }
    });


    $("div.popup-foot > button.btn").on("click", function () {
        layer_popup_close();
    });

    $("div#railroad-map").on("mouseleave",  function () {
        // console.log("mouseleave");
        $("li.li_station").removeClass('on');
    });

});

function layer_popup_close() { // 모달팝업 닫기
    $('.btn-close').parent().parent().parent().css({"display":"none"});
	$("#weekend_section").html();
	$("#weekday_section").html();
}
/*
function showModal(tgid, e){ // 시간표클릭시 모달팝업 띄우기
    // $("#tt_00").show();
    var timerSchedule = $(e).parents(".li_station").children(".select-station").html();
    $(".station_title").html(timerSchedule + "역");
    $(".station").html(timerSchedule + "역");
    $('.dd').css({"display":"block"});
    $("#" + tgid).show();
    $("#weekday_" + tgid.split("_")[1]).css({"display":"block"});
    $("#weekend_" + tgid.split("_")[1]).css({"display":"none"});
}
function weekTime(d, p){
	//d : 주중, p : 주말
    if(d != ""){
        $(d).parents(".contents").children(".weekday").css({"display":"block"});
        $(d).parents(".contents").children(".weekend").css({"display":"none"});
        $(d).addClass("active");
        $(d).next().removeClass("active");
    }
    else if(p != ""){
        $(p).addClass("active");
        $(p).parents(".contents").children(".weekday").css({"display":"none"});
        $(p).parents(".contents").children(".weekend").css({"display":"block"});
        $(p).prev().removeClass("active");
    }
}
*/


function showModalSchedule(tgid, e){ // 시간표 클릭시 모달팝업 띄우기
	
	$("#modal_layer .pop_title").text("시간표");
	$("#weekend_section").html();
	$("#weekday_section").html();
	$("#week_btns").hide();	
	
	//주중데이터
	$.ajax({
		type:"POST",
		url: "/_prog/sttn_info_mng/ajax_sttn_info_schedule.php",
		data: {"sttn_cd" : tgid.split("_")[1], "se" : "weekday" },
		success:function(data){

			
			$("#weekday_section").html(data);

		},

		error:function(e){
			//alert(e.responseText);
			alert("오류가 발생하였습니다.");
		}
		,
		complete:function(){
			$("#modal_layer").show();
		    $("#weekday_section").css({"display":"block"});
		    $("#weekend_section").css({"display":"none"});				
		}
	});
	
	/*
	//주말은 사용하지 않음
	//주말,공휴일데이터
	$.ajax({
		type:"POST",
		url: "/_prog/sttn_info_mng/ajax_sttn_info_schedule.php",
		data: {"sttn_cd" : tgid.split("_")[1], "se" : "weekend"},
		success:function(data){
			
			$("#weekend_section").html(data);
			


		},
		error:function(e){
			//alert(e.responseText);
			alert("오류가 발생하였습니다.");
		}
	});
	*/
	
	//첫 로드시 주중데이터출력
	//$("#weekday_btn").addClass("active");
	//$("#weekend_btn").removeClass("active");
    //$("#weekday_section").css({"display":"block"});
    //$("#weekend_section").css({"display":"none"});			
    //$("#week_btns").show();
  
	}

function weekTime(d, p){
	
	//d : 주중, p : 주말
    if(d != ""){
        $(d).parents(".contents").children(".weekday").css({"display":"block"});
        $(d).parents(".contents").children(".weekend").css({"display":"none"});
        $(d).addClass("active");
        $(d).next().removeClass("active");
    }
    else if(p != ""){
        $(p).addClass("active");
        $(p).parents(".contents").children(".weekday").css({"display":"none"});
        $(p).parents(".contents").children(".weekend").css({"display":"block"});
        $(p).prev().removeClass("active");
    }
}

function showModalStation(tgid, e){ // 역정보 클릭시 모달팝업 띄우기
	
	$("#modal_layer .pop_title").text("역정보");
	$("#weekend_section").html();
	$("#weekday_section").html();
	$("#week_btns").hide();
	
	//역정보
	$.ajax({
		type:"POST",
		url: "/_prog/sttn_info_mng/ajax_sttn_info_schedule.php",
		data: {"sttn_cd" : tgid.split("_")[1], "se" : "station" },
		success:function(data){

			$("#weekday_section").html(data);
			
		},

		error:function(e){
			//alert(e.responseText);
			alert("오류가 발생하였습니다.");
		},
		complete:function(){
			$("#modal_layer").show();
		    $("#weekday_section").css({"display":"block"});
		    $("#weekend_section").css({"display":"none"});
		    
		}
	});
	

		
}

$(document).ready(function(){
    $('#closeModal').on('click', function(){
        $(tgid).css({"display":"none"});
    	$("#weekend_section").html();
    	$("#weekday_section").html();
    	
    });
});



// function tabSwitch(tgid) {
//     $("#" + tgid).show();
//     if (tgid.split("_")[0] == "tt"){
//         $("#" + tgid + " a[href='#weekday_" + tgid.split("_")[1] + "']").trigger("click");
//         $("#" + tgid).find("a.active").focus();
//     }else{
//         $("#" + tgid).find("div.popup-content.scroll").attr("tabindex", -1).focus();
//     }
// }

 function search(info, tp) {
     var alertFlag = true;
     if (typeof info == "undefined") info = station;
     if (typeof tp != "undefined" && tp == "auto") alertFlag = false;

     //역정보가 다 존재할때 적용
     if (info[0] != "" && info[1] != "") {
         var gd = {
             start: info[0],
             arrive: info[1]
         };

         $("#span_time").text(" - ");
         $("#b_distance").text(" - ");
         $("#b_card").text(" - ");
         $("#b_cash").text(" - ");
         $("#span_realtime_title").text("환승역 도착 정보");
         $("#span_realtime_up").text(" - ");
         $("#span_realtime_down").text(" - ");
         /*
         $.getJSON( "/_prog/sttn_info_mng/json_hp_stn_info_sttn_data.php", function( data ) {

     		//console.log(data);	
     		arr_station = data;
     	});
     	*/

     	//주중데이터
     	$.ajax({
     		type:"POST",
     		url: "/_prog/sttn_info_mng/ajax_search_station.php",
     		data: {"start" : info[0], "arrive" : info[1] },
     		success:function(data){

                if (typeof data == "string"){
                	data = JSON.parse(data);
                }

                if (typeof data.routeTime != "undefined"){
                    $("#span_time").text(data.routeTime);
                }
                if (typeof data.routeAway != "undefined") {
                    $("#b_distance").text(data.routeAway);
                }
                /*
                if (typeof data.routeCost != "undefined") {
                    $("#b_card").text(data.routeCost.toString().replace(/(\d)(?=(\d{3})+)/g, '$1,'));
                }
                if (typeof data.routeCostCash != "undefined") {
                    $("#b_cash").text(data.routeCostCash.toString().replace(/(\d)(?=(\d{3})+)/g, '$1,'));
                }
                */
                if (typeof data.routeCostStr != "undefined") {
                    $("#b_cost").text(data.routeCostStr);
                }

     			
     			
     		},

     		error:function(e){
     			//alert(e.responseText);
     			axDialog.alert("정보 조회에 실패했습니다.\n잠시후에 이용해 주세요.");
     		}
     	});

     	//환승역 도착 정보
     	$.ajax({
     		type:"POST",
     		url: "/_prog/sttn_info_mng/ajax_realtime_arrival.php",
     		data: {"start" : info[0], "arrive" : info[1] },
     		success:function(data){

                if (typeof data == "string"){
                	data = JSON.parse(data);
                }

                var upItems = data.items_up || [];
                var downItems = data.items_down || [];
                var lineNm = data.lineNm || "";

                $("#span_realtime_title").text(lineNm != "" ? lineNm+" 환승역 도착 정보" : "환승역 도착 정보");

                $("#span_realtime_up").text(upItems.length > 0 ? upItems.join(" / ") : "도착 정보 없음");
                $("#span_realtime_down").text(downItems.length > 0 ? downItems.join(" / ") : "도착 정보 없음");
     		},

     		error:function(e){
     			$("#span_realtime_title").text("환승역 도착 정보");
     			$("#span_realtime_up").text("도착 정보 없음");
     			$("#span_realtime_down").text("도착 정보 없음");
     		}
     	});

/*
         cmf.stationSearch(gd)
             .done(function (res) {
                 if (typeof res == "string"){
                     res = JSON.parse(res);
                 }

                 if (typeof res.routeTime != "undefined"){
                     $("#span_time").text(res.routeTime);
                 }
                 if (typeof res.routeAway != "undefined") {
                     $("#b_distance").text(res.routeAway);
                 }
                 if (typeof res.routeCost != "undefined") {
                     $("#b_card").text(res.routeCost.toString().replace(/(\d)(?=(\d{3})+)/g, '$1,'));
                 }
                 if (typeof res.routeCostCash != "undefined") {
                     $("#b_cash").text(res.routeCostCash.toString().replace(/(\d)(?=(\d{3})+)/g, '$1,'));
                 }

             })
             .fail(function (res) {
                 axDialog.alert("정보 조회에 실패했습니다.\n잠시후에 이용해 주세요.");
             })
             .always(function () {

             });
*/
     } else {
         if (alertFlag) alert("출발역과 도착역을 지정해 주세요.");
     }
     return false;
 }

// 우리 사이트 역코드(select option value) -> 네이버지도 역 id 매핑
var NAVER_STATION_ID_MAP = {
    "4920": 11711, // 샛강
    "4921": 11712, // 대방(성애병원)
    "4922": 11713, // 서울지방병무청
    "4923": 11714, // 보라매
    "4924": 11715, // 보라매공원
    "4925": 11716, // 보라매병원(전문건설회관)
    "4926": 11717, // 당곡
    "4927": 11718, // 신림
    "4928": 11719, // 서원
    "4929": 11720, // 서울대벤처타운
    "4930": 11721  // 관악산(서울대)
};

function externalMap() {
    var startCd = $("#start-station").val();
    var arriveCd = $("#arrival-station").val();
    var startId = NAVER_STATION_ID_MAP[startCd];
    var arriveId = NAVER_STATION_ID_MAP[arriveCd];

    var vUrl;
    if (startId && arriveId) {
        vUrl = "https://map.naver.com/p/subway/1000/" + startId + "/" + arriveId + "/-/entry/" + startId;
    } else {
        vUrl = "https://map.naver.com/p/subway/lines/1000";
    }

    var vOption = "toolbar=no,menubar=no,location=no,resizable=yes,width=" + screen.width + ",height=" + screen.height + ",left=0,top=0";
    window.open(vUrl, "", vOption);
}

function resetF() {
    station = ["", ""];
    $("#start-station option:eq(0)").prop("selected", true);
    $("#arrival-station option:eq(0)").prop("selected", true);
    if($('ul').find('li.start') || $('ul').find('li.arrival')){
        $('li.li_station').removeClass('arrival').removeClass('start')
    }
    
    clearF();
}

function clearF(){

$("#span_time").text("00분 00초");
$("#b_distance").text("-km");
$("#b_cost").text("-원(교통카드)/-원(일회용승차권)");
$("#span_realtime_title").text("환승역 도착 정보");
$("#span_realtime_up").text("-");
$("#span_realtime_down").text("-");

}


function delayPrintModal(enc_mng_no){
    var filter = "win16|win32|win64|mac|macintel";
    if(filter.indexOf(navigator.platform.toLowerCase()) < 0){
        alert("모바일로 팝업을 띄울수 없습니다.");
    }
    else{
        window.open("/site/kr/html/sub01/popup/print_popup.html?mng_no="+enc_mng_no, "_blank", "toolbar=yes,scrollbars=yes,resizable=yes,top=500,left=1300,width=595,height=840");        
    }
}
function printDelay(){
    $(".btn_box button").remove();
    window.print();
}

// 커스텀 날짜 선택박스 (ul/li)
$(document).ready(function(){
    $(document).on('click', '.custom-select-btn', function(){
        var $wrap = $(this).closest('.custom-select-wrap');
        var isOpen = $wrap.hasClass('open');
        $('.custom-select-wrap').removeClass('open').attr('aria-expanded', 'false');
        if(!isOpen){
            $wrap.addClass('open').attr('aria-expanded', 'true');
        }
    });

    $(document).on('click', '.custom-select-list li', function(){
        var $li = $(this);
        var $wrap = $li.closest('.custom-select-wrap');
        var val = $li.data('value');
        var label = $li.text();
        $li.addClass('selected').attr('aria-selected', 'true')
           .siblings().removeClass('selected').removeAttr('aria-selected');
        $wrap.find('.custom-select-btn').text(label);
        $wrap.find('input[type=hidden]').val(val);
        $wrap.removeClass('open').attr('aria-expanded', 'false');
    });

    $(document).on('click', function(e){
        if(!$(e.target).closest('.custom-select-wrap').length){
            $('.custom-select-wrap').removeClass('open').attr('aria-expanded', 'false');
        }
    });
});

/* ── 메인페이지 경로찾기 위젯에서 파라미터로 진입 시 자동 검색 ── */
$(document).ready(function () {
    var params  = new URLSearchParams(window.location.search);
    var startCd = params.get('start');
    var arrCd   = params.get('arrival');

    if (!startCd || !arrCd) return;

    /* DOM + 이벤트 바인딩 완료 후 직접 처리 */
    setTimeout(function () {
        /* 1. select 값 설정 */
        $('#start-station').val(startCd);
        $('#arrival-station').val(arrCd);

        /* 2. 노선도 li 시각 상태 적용 */
        $('li#station-' + startCd)
            .addClass('start')
            .siblings().removeClass('start');
        $('li#station-' + arrCd)
            .addClass('arrival')
            .siblings().removeClass('arrival');

        /* 3. 전역 station 배열에 직접 세팅 */
        station[0] = parseInt(startCd, 10);
        station[1] = parseInt(arrCd, 10);

        /* 4. 검색 실행 */
        search(station, 'auto');

        /* 5. 결과 영역으로 스크롤 */
        setTimeout(function () {
            var $result = $('.search_result');
            if ($result.length) {
                $('html, body').animate({ scrollTop: $result.offset().top - 80 }, 400);
            }
        }, 600);
    }, 400);
});